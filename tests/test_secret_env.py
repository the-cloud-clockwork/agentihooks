import base64
import io
import shlex
import stat
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from hooks import config
from hooks.context import credential_guard
from scripts import secret_env
from scripts.cli_delegates import delegated_cli

pytestmark = pytest.mark.feature("cluster-secret-private-env")


def _args(target: Path) -> list[str]:
    return ["example", "service", "token.key", "SERVICE_TOKEN", str(target)]


def _cluster(monkeypatch: pytest.MonkeyPatch, value: str, code: int = 0) -> Mock:
    result = subprocess.CompletedProcess([], code, base64.b64encode(value.encode()), value.encode())
    run = Mock(return_value=result)
    monkeypatch.setattr(subprocess, "run", run)
    return run


@pytest.mark.parametrize(
    "suffix,blocked",
    [
        ("", False),
        ("; cat ~/.agentihooks/private.env", True),
        ("; kubectl get secret service -o json", True),
        (" $(cat ~/.agentihooks/private.env)", True),
    ],
    ids=["transfer", "file-read", "secret-read", "nested-read"],
)
def test_guard_keeps_the_transfer_and_reader_boundaries(suffix: str, blocked: bool) -> None:
    command = "agentihooks secret-env example service token.key SERVICE_TOKEN ~/.agentihooks/private.env"
    verdict = credential_guard.decide({"tool_name": "Bash", "tool_input": {"command": command + suffix}})
    assert (verdict is not None) is blocked


def test_the_cli_delegates_the_transfer() -> None:
    assert delegated_cli(["secret-env"]) is secret_env.main


def test_transfer_creates_a_private_file_without_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    target = tmp_path / "private.env"
    value = 'synthetic "value" $literal ' + chr(96) + "literal" + chr(96)
    run = _cluster(monkeypatch, value)

    assert secret_env.main(_args(target)) == 0
    assert target.read_text() == f"SERVICE_TOKEN={shlex.quote(value)}\n"
    monkeypatch.delenv("SERVICE_TOKEN", raising=False)
    assert config._parse_env_file(target) == set()
    assert secret_env.os.environ["SERVICE_TOKEN"] == value
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    run.assert_called_once_with(
        [
            "kubectl",
            "get",
            "secret",
            "service",
            "--namespace",
            "example",
            "-o",
            'go-template={{index .data "token.key"}}',
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert capsys.readouterr() == ("", "")


def test_transfer_replaces_duplicate_assignments_and_preserves_other_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    target = tmp_path / "private.env"
    target.write_text("OTHER=keep\nexport SERVICE_TOKEN=old\nSERVICE_TOKEN=duplicate\nLAST=stay")
    target.chmod(0o644)
    _cluster(monkeypatch, "replacement")

    assert secret_env.main(_args(target)) == 0
    assert target.read_text() == "OTHER=keep\nSERVICE_TOKEN=replacement\nLAST=stay\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "kind",
    [
        "outside",
        "parent-symlink",
        "target-symlink",
        "directory",
        "home",
        "code-file",
        "missing-parent",
        "inside-symlink",
    ],
)
def test_transfer_refuses_unsafe_targets_before_reading_the_cluster(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(state))
    target = state / "private.env"
    if kind == "outside":
        target = tmp_path / "outside.env"
    elif kind == "parent-symlink":
        (state / "link").symlink_to(tmp_path, target_is_directory=True)
        target = state / "link" / "private.env"
    elif kind == "target-symlink":
        target.symlink_to(tmp_path / "outside.env")
    elif kind == "home":
        target = state
    elif kind == "code-file":
        target = state / "settings.py"
    elif kind == "missing-parent":
        target = state / "missing" / "private.env"
    elif kind == "inside-symlink":
        target.symlink_to(state / "other.env")
    else:
        target.mkdir()
    run = _cluster(monkeypatch, "synthetic-value")
    assert secret_env.main(_args(target)) == 1
    run.assert_not_called()
    assert capsys.readouterr() == ("", "Secret transfer failed.\n")


@pytest.mark.parametrize("index,value", [(0, "--bad"), (1, "--bad"), (2, 'bad"key'), (3, "BAD-NAME")])
def test_transfer_refuses_invalid_identifiers(
    index: int, value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    args = _args(tmp_path / "private.env")
    args[index] = value
    run = _cluster(monkeypatch, "synthetic-value")
    assert secret_env.main(args) == 1
    run.assert_not_called()
    assert capsys.readouterr() == ("", "Secret transfer failed.\n")


@pytest.mark.parametrize("failure", ["exit", "timeout", "missing", "invalid", "multiline", "nul"])
def test_failed_retrieval_never_writes_or_exposes_values(
    failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    target = tmp_path / "private.env"
    target.write_text("OTHER=keep\n")
    run = _cluster(monkeypatch, "synthetic-value", 1 if failure == "exit" else 0)
    if failure == "timeout":
        run.side_effect = subprocess.TimeoutExpired("kubectl", 30, b"synthetic-value", b"synthetic-value")
    elif failure in {"missing", "invalid"}:
        run.return_value = subprocess.CompletedProcess(
            [], 0, b"<no value>" if failure == "missing" else b"!invalid", b""
        )
    elif failure in {"multiline", "nul"}:
        value = "synthetic\nvalue" if failure == "multiline" else "synthetic\x00value"
        run.return_value = subprocess.CompletedProcess([], 0, base64.b64encode(value.encode()), b"")

    assert secret_env.main(_args(target)) == 1
    assert target.read_text() == "OTHER=keep\n"
    assert capsys.readouterr() == ("", "Secret transfer failed.\n")


@pytest.mark.parametrize("value", ["", "caf\u00e9 synthetic"])
def test_transfer_appends_to_a_relative_file(value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    target = tmp_path / "private.env"
    target.write_text("OTHER=café\n", encoding="utf-8")

    def text_encoding(encoding: str | None, _stacklevel: int = 2) -> str:
        return "latin-1" if encoding is None else encoding

    monkeypatch.setattr(io, "text_encoding", text_encoding)
    _cluster(monkeypatch, value)
    assert secret_env.main(_args(Path("private.env"))) == 0
    assert target.read_text(encoding="utf-8") == f"OTHER=café\nSERVICE_TOKEN={shlex.quote(value)}\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_transfer_uses_the_default_agentihooks_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTIHOOKS_HOME", raising=False)
    state = Path.home() / ".agentihooks"
    state.mkdir(parents=True, exist_ok=True)
    target = state / ".env"
    _cluster(monkeypatch, "synthetic-value")
    assert secret_env.main(_args(target)) == 0
    assert target.read_text() == "SERVICE_TOKEN=synthetic-value\n"


def test_failed_replace_preserves_the_file_and_cleans_the_private_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    target = tmp_path / "private.env"
    target.write_text("OTHER=keep\n")
    _cluster(monkeypatch, "synthetic-value")
    monkeypatch.setattr(secret_env.os, "replace", Mock(side_effect=OSError("synthetic-value")))
    assert secret_env.main(_args(target)) == 1
    assert target.read_text() == "OTHER=keep\n"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["private.env"]
    assert capsys.readouterr() == ("", "Secret transfer failed.\n")


@pytest.mark.parametrize("quote", ["'", '"'])
def test_existing_multiline_values_are_refused_without_changes(
    quote: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    target = tmp_path / "private.env"
    original = f"OTHER={quote}first\nSERVICE_TOKEN=inside\nlast{quote}\nSERVICE_TOKEN=old\n"
    target.write_text(original)
    _cluster(monkeypatch, "replacement")
    assert secret_env.main(_args(target)) == 1
    assert target.read_text() == original
    assert capsys.readouterr() == ("", "Secret transfer failed.\n")


def test_apostrophes_round_trip_through_the_host_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.delenv("SERVICE_TOKEN", raising=False)
    target = tmp_path / "private.env"
    _cluster(monkeypatch, "synthetic 'value'")
    assert secret_env.main(_args(target)) == 0
    assert target.read_text() == "SERVICE_TOKEN=\"synthetic 'value'\"\n"
    assert config._parse_env_file(target) == set()
    assert secret_env.os.environ["SERVICE_TOKEN"] == "synthetic 'value'"
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "value",
    [
        "synthetic 'value' $literal",
        """synthetic 'value' "literal" """,
        "synthetic 'value' \\",
        "synthetic 'value' " + chr(96),
    ],
)
def test_unrepresentable_values_are_refused_without_changes(
    value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    target = tmp_path / "private.env"
    target.write_text("OTHER=keep\n")
    _cluster(monkeypatch, value)
    assert secret_env.main(_args(target)) == 1
    assert target.read_text() == "OTHER=keep\n"
    assert capsys.readouterr() == ("", "Secret transfer failed.\n")


def test_usage_is_fixed_and_does_not_read_the_cluster(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _cluster(monkeypatch, "synthetic-value")
    assert secret_env.main([]) == 2
    run.assert_not_called()
    assert capsys.readouterr() == ("", "Usage: agentihooks secret-env NAMESPACE SECRET KEY VARIABLE FILE\n")
