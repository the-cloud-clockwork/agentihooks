import json
from pathlib import Path

import pytest

from scripts import codex_router
from scripts.routing import native_default

pytestmark = pytest.mark.unit


@pytest.mark.feature("quick-native-authentication")
@pytest.mark.parametrize("configured_state", [True, False])
@pytest.mark.parametrize(
    ("agent", "variable", "filename"),
    [("claude", "CLAUDE_CONFIG_DIR", ".credentials.json"), ("codex", "CODEX_HOME", "auth.json")],
)
def test_native_default_isolates_local_login_from_master_and_routed_credentials(
    agent: str, variable: str, filename: str, configured_state: bool, tmp_path: Path
) -> None:
    import hashlib

    operator = tmp_path / f".{agent}"
    operator.mkdir()
    (operator / filename).write_bytes(b"local-login-fixture")
    master = tmp_path / "master"
    master.mkdir()
    (master / filename).write_bytes(b"master-login-fixture")
    (master / "settings.json").write_text("{}")
    environ = {
        "HOME": str(tmp_path),
        "AGENTIHOOKS_HOME": str(tmp_path / "state"),
        variable: str(master),
        "AH_CC_TOKEN_alpha": "unused",
        "AH_CX_TOKEN_beta": "unused",
        "CLAUDE_CODE_OAUTH_TOKEN": "unused",
        "OPENAI_API_KEY": "unused",
        "ANTHROPIC_API_KEY": "unused",
        "AH_ROUTE_INTERACTIVE_master": "1",
        "AGENTIHOOKS_ROUTE_ACCOUNT": "master",
    }
    state = tmp_path / "state" if configured_state else tmp_path / ".agentihooks"
    if not configured_state:
        environ.pop("AGENTIHOOKS_HOME")
    child = native_default.prepare(agent, environ)
    home = Path(child[variable])
    assert home.is_relative_to(state / "profiles")
    assert home.parent.name.startswith(f"{hashlib.sha256(b'{}').hexdigest()[:12]}-")
    assert home != master and home != operator
    assert (home / filename).read_bytes() == b"local-login-fixture"
    assert not (home / filename).is_symlink()
    assert home.stat().st_mode & 0o777 == 0o700
    assert (home / filename).stat().st_mode & 0o777 == 0o600
    assert (home / "settings.json").read_text() == "{}"
    assert (operator / filename).read_bytes() == b"local-login-fixture"
    assert (master / filename).read_bytes() == b"master-login-fixture"
    assert not any(name.startswith(("AH_CC_TOKEN_", "AH_CX_TOKEN_", "AH_ROUTE_INTERACTIVE_")) for name in child)
    assert "AGENTIHOOKS_ROUTE_ACCOUNT" not in child
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in child
    assert "OPENAI_API_KEY" not in child and "ANTHROPIC_API_KEY" not in child


@pytest.mark.feature("quick-native-authentication")
def test_codex_native_default_bypasses_automatic_account_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import subprocess

    from scripts import deps_preflight
    from scripts.hive import account_slots

    def refuse_route(*args: object, **kwargs: object) -> None:
        pytest.fail("native default enumerated routed accounts")

    monkeypatch.setattr(codex_router, "_route", refuse_route)
    monkeypatch.setattr(codex_router.shutil, "which", lambda name: name)
    monkeypatch.setattr(deps_preflight, "refresh_codex_wrapper", lambda name: None)
    monkeypatch.setattr(account_slots, "execute", lambda harness, account, environ, run: run(environ))
    operator = tmp_path / ".codex"
    operator.mkdir()
    (operator / "auth.json").write_bytes(b"local-login-fixture")
    calls: list[dict[str, str]] = []
    checked: list[dict[str, str]] = []

    def signed_in(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert args == (["codex", "login", "status"],)
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert (Path(environment["CODEX_HOME"]) / "auth.json").read_bytes() == b"local-login-fixture"
        assert kwargs["capture_output"] is True and kwargs["timeout"] == 30
        checked.append(environment)
        return subprocess.CompletedProcess(["codex", "login", "status"], 0, stdout=b"")

    def execute(binary: str, args: list[str], environ: dict[str, str]) -> None:
        assert binary == "codex"
        calls.append(environ)

    report = tmp_path / "route"
    assert (
        codex_router.main(
            ["--route", "native-default", "--agentihooks-report", str(report)],
            {"HOME": str(tmp_path), "AGENTIHOOKS_HOME": str(tmp_path / "state")},
            execvpe=execute,
            run=signed_in,
        )
        == 0
    )
    assert len(calls) == len(checked) == 1
    assert Path(calls[0]["CODEX_HOME"]) != operator
    assert checked[0]["CODEX_HOME"] == calls[0]["CODEX_HOME"]
    assert dict(line.split("=", 1) for line in report.read_text().splitlines()) == {
        "status": "routed",
        "account": "default",
        "placement": "forced",
    }
    assert capsys.readouterr().out == "[agentihooks codex] account=default sessions=0/? placement=forced\n"


@pytest.mark.feature("quick-native-authentication")
@pytest.mark.parametrize("failure", ["prepare", "signed-out"])
def test_codex_native_default_refuses_failures_without_starting_an_agent(
    failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import subprocess

    ordinary = tmp_path / ".codex"
    ordinary.mkdir()
    (ordinary / "auth.json").write_bytes(b"local-login-fixture")
    message = "preparation fixture failed" if failure == "prepare" else "native default login is unavailable"

    def prepare(*args: object, **kwargs: object) -> dict[str, str]:
        raise OSError(message)

    def signed_out(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess([], 1)

    def execute(*args: object, **kwargs: object) -> None:
        pytest.fail("refused native default launch executed")

    if failure == "prepare":
        monkeypatch.setattr(native_default, "prepare", prepare)
    report = tmp_path / "route"
    assert (
        codex_router.main(
            ["--route", "native-default", "--agentihooks-report", str(report)],
            {"HOME": str(tmp_path)},
            execvpe=execute,
            run=signed_out,
        )
        == 3
    )
    assert capsys.readouterr().err == f"agentihooks codex: {message}\n"
    assert dict(line.split("=", 1) for line in report.read_text().splitlines()) == {
        "status": "failed",
        "error": message,
    }


@pytest.mark.feature("quick-native-authentication")
@pytest.mark.parametrize(
    ("agent", "variable", "credential", "persona"),
    [
        ("claude", "CLAUDE_CONFIG_DIR", ".credentials.json", "CLAUDE.md"),
        ("codex", "CODEX_HOME", "auth.json", "AGENTS.md"),
    ],
)
def test_native_default_preserves_and_repins_rendered_profile_binding(
    agent: str, variable: str, credential: str, persona: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from scripts.profiles import binding

    ordinary = tmp_path / f".{agent}"
    ordinary.mkdir()
    (ordinary / credential).write_bytes(b"local-login-fixture")
    source = tmp_path / "rendered" / agent
    source.mkdir(parents=True)
    (source / persona).write_text("Engineer --canary abcdef0123456789abcdef01")
    (source.parent / f"{agent}.sources.json").write_text(
        json.dumps([{"locator": {"repo": "/fixture/source", "path": "profile", "blob": "rendered"}}])
    )
    monkeypatch.setattr(
        binding.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="original")
    )
    (source / "state").mkdir()
    (source / "state" / "retained").write_text("shared state")
    (source / "plans").mkdir()
    (source / "plans" / "retained.md").write_text("saved plan")
    binding.write(source, "engineer", agent)
    original = binding.inspect(source, "engineer", agent)
    report = tmp_path / "request.json"
    binding.request(report, "engineer", agent, source)
    monkeypatch.setattr(
        binding.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="advanced")
    )
    child = native_default.prepare(
        agent,
        {
            "HOME": str(tmp_path),
            "AGENTIHOOKS_HOME": str(tmp_path / "state"),
            variable: str(source),
            "AGENTIHOOKS_PROFILE": "engineer",
            binding.REPORT: str(report),
        },
    )
    home = Path(child[variable])
    assert binding.inspect(home, "engineer", agent) == {**original, "home": str(home.resolve())}
    assert json.loads(report.read_text()) == {
        "profile": "engineer",
        "harness": agent,
        "state": "pending",
        "rendered": {**original, "home": str(home.resolve())},
    }
    assert not (home / "plans").is_symlink()
    assert (home / "plans" / "retained.md").read_text() == "saved plan"
    assert (home / "state").is_symlink()
    assert (home / "state" / "retained").read_text() == "shared state"
    assert binding.inspect(source, "engineer", agent) == original


@pytest.mark.feature("quick-native-authentication")
def test_native_default_configuration_omits_logins_and_preserves_plan_links(tmp_path: Path) -> None:
    from scripts.profiles import binding

    source, home = tmp_path / "source", tmp_path / "home"
    source.mkdir()
    home.mkdir()
    for name in (".credentials.json", "auth.json", binding.FILE):
        (source / name).write_text("ignored fixture")
    (source / "settings.json").write_text("{}")
    (source / "plans").mkdir()
    (source / "plans" / "link").symlink_to("missing-plan.md")
    (source / "state").mkdir()
    native_default._configuration(source, home)
    assert {entry.name for entry in home.iterdir()} == {"settings.json", "plans", "state"}
    assert (home / "settings.json").read_text() == "{}"
    assert not (home / "plans").is_symlink()
    assert (home / "plans" / "link").is_symlink()
    assert (home / "plans" / "link").readlink() == Path("missing-plan.md")
    assert (home / "state").is_symlink()
    assert (home / "state").resolve() == (source / "state").resolve()


@pytest.mark.feature("quick-native-authentication")
@pytest.mark.parametrize(("profile", "recorded"), [("", True), ("engineer", False)])
def test_native_default_binding_requires_profile_and_rendered_evidence(
    profile: str, recorded: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.profiles import binding

    source, home = tmp_path / "source", tmp_path / "home"
    source.mkdir()
    home.mkdir()
    if recorded:
        (source / binding.FILE).write_text("{}")

    def refuse(*args: object, **kwargs: object) -> None:
        pytest.fail("unrendered native configuration was inspected")

    monkeypatch.setattr(binding, "inspect", refuse)
    native_default._binding("claude", source, home, {"AGENTIHOOKS_PROFILE": profile})
    assert list(home.iterdir()) == []


@pytest.mark.feature("quick-native-authentication")
@pytest.mark.parametrize("message", ["unavailable fixture", "invalid fixture"])
def test_claude_native_default_preserves_preparation_failures(
    message: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.claude_quota_balancer import RoutingError

    def refuse(*args: object, **kwargs: object) -> dict[str, str]:
        raise ValueError(message)

    monkeypatch.setattr(native_default, "prepare", refuse)
    report = tmp_path / "route"
    with pytest.raises(RoutingError) as error:
        native_default.launch_claude("claude", [], {"HOME": str(tmp_path)}, str(report))
    assert str(error.value) == message
    assert isinstance(error.value.__cause__, ValueError)
    assert not report.exists()


@pytest.mark.feature("quick-native-authentication")
@pytest.mark.parametrize(("agent", "variable"), [("claude", "CLAUDE_CONFIG_DIR"), ("codex", "CODEX_HOME")])
def test_native_default_missing_local_login_never_uses_the_master(agent: str, variable: str, tmp_path: Path) -> None:
    master = tmp_path / "master"
    master.mkdir()
    with pytest.raises(ValueError, match="^native default login is unavailable$"):
        native_default.prepare(agent, {"HOME": str(tmp_path), variable: str(master)})


@pytest.mark.feature("quick-native-authentication")
def test_claude_native_default_checks_and_launches_the_isolated_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    from collections.abc import Callable
    from io import StringIO

    from scripts.hive import account_slots

    ordinary = tmp_path / ".claude"
    ordinary.mkdir()
    (ordinary / ".credentials.json").write_bytes(b"local-login-fixture")
    checked: list[str] = []
    calls: list[tuple[str, list[str], dict[str, str]]] = []
    stream = StringIO()
    flushed: list[str] = []

    def flush() -> None:
        flushed.append(stream.getvalue())

    def check(environ: dict[str, str], account: str) -> dict[str, str]:
        checked.append(account)
        assert (Path(environ["CLAUDE_CONFIG_DIR"]) / ".credentials.json").read_bytes() == b"local-login-fixture"
        return {}

    def execute(binary: str, args: list[str], environ: dict[str, str]) -> None:
        calls.append((binary, args, environ))

    def reserve(harness: str, account: str, environ: dict[str, str], run: Callable[[dict[str, str]], None]) -> None:
        assert (harness, account) == ("claude", "default")
        assert flushed == ["[agenti] account=default route=default\n"]
        run(environ)

    monkeypatch.setattr(native_default.native_login, "check", check)
    monkeypatch.setattr(native_default.os, "execvpe", execute)
    monkeypatch.setattr(account_slots, "execute", reserve)
    monkeypatch.setattr(stream, "flush", flush)
    report = tmp_path / "route.json"
    with monkeypatch.context() as output:
        output.setattr(sys, "stdout", stream)
        native_default.launch_claude("claude-fixture", ["--", "hello"], {"HOME": str(tmp_path)}, str(report))
    assert checked == ["default"]
    assert len(calls) == 1 and calls[0][0] == "claude-fixture"
    assert calls[0][1] == ["claude-fixture", "--dangerously-skip-permissions", "--", "hello"]
    assert Path(calls[0][2]["CLAUDE_CONFIG_DIR"]) != ordinary
    assert dict(line.split("=", 1) for line in report.read_text().splitlines()) == {
        "status": "routed",
        "account": "default",
        "placement": "forced",
    }
    assert stream.getvalue() == "[agenti] account=default route=default\n"


@pytest.mark.feature("native-default-codex-hook-trust")
@pytest.mark.parametrize("layout", ["table", "inline", "dotted"])
def test_codex_native_default_rebases_hook_trust_without_other_byte_changes(tmp_path: Path, layout: str) -> None:
    import tomllib

    source = tmp_path / ".codex"
    source.mkdir()
    (source / "auth.json").write_bytes(b"native-login-fixture")
    (source / "hooks.json").write_text("{}")
    hook = source / "hooks.json"
    key = f"{hook}:SessionStart:0:0"
    header = f"# keep formatting\r\nmodel = 'native'\r\npath = '{key}'\r\n"
    layouts = {
        "table": f"[ hooks.state.'{key}' ]\r\ntrusted_hash  = 'hash-one' # retained\r\nenabled = false\r\n",
        "inline": f"[hooks.state]\r\n'{key}'  = {{ trusted_hash = 'hash-one', enabled = false }} # retained\r\n",
        "dotted": f"hooks.state.'{key}'.trusted_hash  = 'hash-one' # retained\r\nhooks.state.'{key}'.enabled = false\r\n",
    }
    foreign = "[hooks.state.'foreign:SessionStart:0:0']\r\ntrusted_hash = 'foreign-hash'\r\n"
    original = (header + layouts[layout] + foreign).encode()
    (source / "config.toml").write_bytes(original)
    child = native_default.prepare("codex", {"HOME": str(tmp_path)})
    home = Path(child["CODEX_HOME"])
    copied = (home / "config.toml").read_bytes()
    expected = original.replace(f"'{key}'".encode(), f"'{home / 'hooks.json'}:SessionStart:0:0'".encode())
    assert copied == expected
    state = tomllib.loads(copied.decode())["hooks"]["state"]
    assert state[f"{home / 'hooks.json'}:SessionStart:0:0"] == {"trusted_hash": "hash-one", "enabled": False}
    assert state["foreign:SessionStart:0:0"] == {"trusted_hash": "foreign-hash"}
    assert (source / "config.toml").read_bytes() == original


@pytest.mark.feature("native-default-codex-hook-trust")
def test_codex_native_default_rebases_both_symlink_hook_spellings(tmp_path: Path) -> None:
    source = tmp_path / ".codex"
    source.mkdir()
    (source / "auth.json").write_bytes(b"native-login-fixture")
    hooks = tmp_path / "shared-hooks.json"
    hooks.write_text("{}")
    (source / "hooks.json").symlink_to(hooks)
    original = (
        f'[hooks.state."{source / "hooks.json"}:SessionStart:0:0"]\ntrusted_hash = "source"\n'
        f'[hooks.state."{hooks}:Stop:0:0"]\ntrusted_hash = "resolved"\n'
        f'[hooks.state."{source / "hooks.json"}.other:Stop:0:0"]\ntrusted_hash = "similar"\n'
    ).encode()
    (source / "config.toml").write_bytes(original)
    child = native_default.prepare("codex", {"HOME": str(tmp_path)})
    home = Path(child["CODEX_HOME"])
    expected = original.replace(f'"{source / "hooks.json"}:'.encode(), f'"{home / "hooks.json"}:'.encode())
    expected = expected.replace(f'"{hooks}:'.encode(), f'"{home / "hooks.json"}:'.encode())
    assert (home / "config.toml").read_bytes() == expected
    assert (home / "hooks.json").read_bytes() == hooks.read_bytes()
    assert not (home / "hooks.json").is_symlink()


@pytest.mark.feature("native-default-codex-hook-trust")
def test_claude_native_default_preserves_codex_looking_configuration_bytes(tmp_path: Path) -> None:
    source = tmp_path / ".claude"
    source.mkdir()
    (source / ".credentials.json").write_bytes(b"native-login-fixture")
    original = f'# untouched\r\n[ hooks.state."{source / "hooks.json"}:Stop:0:0" ]\r\ntrusted_hash="same"\r\n'.encode()
    (source / "config.toml").write_bytes(original)
    child = native_default.prepare("claude", {"HOME": str(tmp_path)})
    assert (Path(child["CLAUDE_CONFIG_DIR"]) / "config.toml").read_bytes() == original


@pytest.mark.feature("native-default-codex-hook-trust")
@pytest.mark.parametrize(
    "config",
    [
        None,
        b"# no hooks\r\nmodel='native'\r\n",
        b"[hooks]\r\nenabled=true\r\n",
        b'[hooks.state."foreign:Stop:0:0"]\r\ntrusted_hash="same"\r\n',
    ],
    ids=["absent-config", "absent-hooks", "absent-state", "foreign-state"],
)
def test_codex_native_default_preserves_configuration_without_matching_trust(
    tmp_path: Path, config: bytes | None
) -> None:
    source = tmp_path / ".codex"
    source.mkdir()
    (source / "auth.json").write_bytes(b"native-login-fixture")
    if config is not None:
        (source / "config.toml").write_bytes(config)
    child = native_default.prepare("codex", {"HOME": str(tmp_path)})
    target = Path(child["CODEX_HOME"]) / "config.toml"
    if config is None:
        assert not target.exists()
    else:
        assert target.read_bytes() == config
