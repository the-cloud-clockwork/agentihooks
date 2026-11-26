import json
from pathlib import Path

import pytest

from scripts import codex_router
from scripts.routing import native_default

pytestmark = pytest.mark.unit


@pytest.mark.feature("quick-native-authentication")
@pytest.mark.parametrize(
    ("agent", "variable", "filename"),
    [("claude", "CLAUDE_CONFIG_DIR", ".credentials.json"), ("codex", "CODEX_HOME", "auth.json")],
)
def test_native_default_isolates_local_login_from_master_and_routed_credentials(
    agent: str, variable: str, filename: str, tmp_path: Path
) -> None:
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
    child = native_default.prepare(agent, environ)
    home = Path(child[variable])
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse_route(*args: object, **kwargs: object) -> None:
        pytest.fail("native default enumerated routed accounts")

    monkeypatch.setattr(codex_router, "_route", refuse_route)
    monkeypatch.setattr(codex_router, "default_signed_in", lambda environ, run: True)
    from scripts import deps_preflight

    monkeypatch.setattr(deps_preflight, "refresh_codex_wrapper", lambda name: None)
    operator = tmp_path / ".codex"
    operator.mkdir()
    (operator / "auth.json").write_bytes(b"local-login-fixture")
    calls: list[dict[str, str]] = []

    def execute(binary: str, args: list[str], environ: dict[str, str]) -> None:
        calls.append(environ)

    assert (
        codex_router.main(
            ["--route", "native-default"],
            {"HOME": str(tmp_path), "AGENTIHOOKS_HOME": str(tmp_path / "state")},
            execvpe=execute,
        )
        == 0
    )
    assert len(calls) == 1
    assert Path(calls[0]["CODEX_HOME"]) != operator


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
@pytest.mark.parametrize(("agent", "variable"), [("claude", "CLAUDE_CONFIG_DIR"), ("codex", "CODEX_HOME")])
def test_native_default_missing_local_login_never_uses_the_master(agent: str, variable: str, tmp_path: Path) -> None:
    master = tmp_path / "master"
    master.mkdir()
    with pytest.raises(ValueError, match="^native default login is unavailable$"):
        native_default.prepare(agent, {"HOME": str(tmp_path), variable: str(master)})


@pytest.mark.feature("quick-native-authentication")
def test_claude_native_default_checks_and_launches_the_isolated_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.hive import account_slots

    ordinary = tmp_path / ".claude"
    ordinary.mkdir()
    (ordinary / ".credentials.json").write_bytes(b"local-login-fixture")
    checked: list[str] = []
    calls: list[tuple[str, list[str], dict[str, str]]] = []

    def check(environ: dict[str, str], account: str) -> dict[str, str]:
        checked.append(account)
        assert (Path(environ["CLAUDE_CONFIG_DIR"]) / ".credentials.json").read_bytes() == b"local-login-fixture"
        return {}

    def execute(binary: str, args: list[str], environ: dict[str, str]) -> None:
        calls.append((binary, args, environ))

    monkeypatch.setattr(native_default.native_login, "check", check)
    monkeypatch.setattr(native_default.os, "execvpe", execute)
    monkeypatch.setattr(account_slots, "execute", lambda harness, account, environ, run: run(environ))
    report = tmp_path / "route.json"
    native_default.launch_claude("claude-fixture", ["--", "hello"], {"HOME": str(tmp_path)}, str(report))
    assert checked == ["default"]
    assert len(calls) == 1 and calls[0][0] == "claude-fixture"
    assert calls[0][1][-2:] == ["--", "hello"]
    assert Path(calls[0][2]["CLAUDE_CONFIG_DIR"]) != ordinary
    assert dict(line.split("=", 1) for line in report.read_text().splitlines()) == {
        "status": "routed",
        "account": "default",
        "placement": "forced",
    }
    assert capsys.readouterr().out == "[agenti] account=default route=default\n"
