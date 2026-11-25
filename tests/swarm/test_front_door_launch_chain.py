"""The front door master launch in its deployed shape: the swarm runtime's spawn, the init-agent launcher script, the
agentihooks claude entry and the claude ai login route run unstubbed between the login check and the process exec."""

import contextlib
import io
import json
import os
import shlex
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hooks.context import account_sessions
from scripts import herdr_host, init_agent, install, master_logins, openbao_kv, select_profile
from scripts import master_claude_login as login
from scripts.profiles import binding
from scripts.routing import native_login
from scripts.swarm import remote_control
from scripts.swarm.runtime import HerdrRuntime, parse_fields

pytestmark = pytest.mark.unit

TITLE = "master 1 · proof"
RC = ["--remote-control", TITLE, "--remote-control-session-name-prefix", "hive-a"]
PANE = {
    "AGENTIHOOKS_PROFILE": "master",
    "AGENTIHOOKS_AGENT_NAME": "master@proof",
    "AH_CC_TOKEN_workeratexample_com": "fixture",
    "CLAUDE_CODE_OAUTH_TOKEN": "fixture",
    "SWARM_HIVE_ID": "hive-a",
}
DEAD_HOLDER = 999_999_999
MARK = "AGENTIHOOKS_ENV_FILE_MARK"
ENV_FILE = {"CLAUDE_CODE_OAUTH_TOKEN": "env-file", "AH_CC_TOKEN_workeratexample_com": "env-file", MARK: "loaded"}
Exec = tuple[str, list[str], dict[str, str]]


@pytest.fixture
def executed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[Exec]:
    """Two stored claude ai logins, a rendered master profile whose binding evidence the launch requests, a pane
    environment and an agentihooks env file both holding worker tokens, and every process boundary recorded."""
    from scripts import claude_quota_balancer, deps_preflight, profile_telemetry
    from scripts.hive import account_slots
    from scripts.scheduler import service

    monkeypatch.setattr(login.hooks_config, "AGENTIHOOKS_HOME", tmp_path / "ah")
    monkeypatch.setattr(login.hooks_config, "MASTER_CLAUDE_LOGIN", True)
    monkeypatch.setattr(login, "MANAGED", tmp_path / "no-managed.json")
    monkeypatch.setattr(openbao_kv, "from_env", lambda _: None)
    monkeypatch.setattr(master_logins, "left", lambda *_: 80.0)
    monkeypatch.setattr(native_login, "check", lambda *_: dict(native_login.PROOF))
    monkeypatch.setenv(login.ORDER_ENV, "one@example.com,two@example.com")
    rendered = tmp_path / "profiles" / "master" / "claude"
    rendered.mkdir(parents=True)
    (rendered / "settings.json").write_text("{}")
    (rendered / login.CONFIG).write_text("{}")
    (rendered / binding.PERSONAS["claude"]).write_text("persona\n")
    (rendered.parent / "claude.sources.json").write_text("[]")
    for number, email in ((1, "one@example.com"), (2, "two@example.com")):
        target = login.home(number)
        target.mkdir(parents=True)
        (target / login.CREDENTIALS).write_text(json.dumps({login.GRANT: {"refreshToken": "fixture"}}))
        (target / login.CONFIG).write_text(json.dumps({login.ACCOUNT: {"emailAddress": email}}))
        (target.parent / login.OWNER).write_text(json.dumps({"holders": {"master@old": DEAD_HOLDER}, "epoch": 3}))
    for name, value in {**PANE, "CLAUDE_CONFIG_DIR": str(rendered)}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(binding.REPORT, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(herdr_host, "binary", lambda: None)
    monkeypatch.setattr(profile_telemetry, "installed_langfuse_env", lambda target: {})
    monkeypatch.setattr(select_profile, "prepare", lambda *args: _render(rendered, *args))
    monkeypatch.setattr(service, "ensure_running", lambda environ: False)
    monkeypatch.setattr(init_agent, "_launch_command", lambda launcher, *_: ("linux", ["terminal", str(launcher)]))
    monkeypatch.setattr(install.shutil, "which", lambda name: f"/opt/{name}")
    state = tmp_path / "state"
    state.mkdir()
    (state / ".env").write_text("".join(f"{name}={value}\n" for name, value in ENV_FILE.items()))
    monkeypatch.setattr(install, "_ENV_FILE_DST", state / ".env")
    monkeypatch.setenv(MARK, "pane")
    monkeypatch.setattr(deps_preflight, "ensure", lambda: None)
    monkeypatch.setattr(claude_quota_balancer, "_cache_path", lambda _: tmp_path / "quota.json")
    monkeypatch.setattr(account_slots, "execute", lambda harness, account, environ, invoke, **_: invoke(environ))
    seen: list[Exec] = []
    monkeypatch.setattr(os, "execvpe", lambda binary, argv, environ: seen.append((binary, argv, environ)))
    return seen


def _pane(tmp_path: Path, executed: list[Exec], argv: list[str], reports: list[str]) -> SimpleNamespace:
    """Run what the herdr pane runs for an init-agent launch, with a terminal start that runs the launcher in process
    and a canary that answers for the claude process it execs, and return the report init-agent prints."""
    args = ["--canary-timeout", "1", *argv[argv.index("init-agent") + 1 :]]
    at = args.index("--host")
    del args[at : at + 2]
    environ = {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "runtime")}
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = init_agent.main(args, environ, start=_terminal, canary=lambda report, _: _canary(report, executed))
    reports.append(out.getvalue())
    return SimpleNamespace(returncode=code, stdout=out.getvalue(), stderr=err.getvalue())


def _render(
    home: Path, name: str, agent: str, model: str, effort: str, agent_args: list[str], *_: Any
) -> tuple[dict[str, str], list[str]]:
    """Render the requested profile's binding evidence into the stored home, the way select-profile prepares it."""
    binding.write(home, name, agent)
    return {"AGENTIHOOKS_PROFILE": "master", binding.HOMES[agent]: str(home)}, agent_args


def _terminal(command: list[str], environ: dict[str, str]) -> None:
    """Start the launcher the terminal command names: write its started marker and run its harness line in process."""
    assert Path(environ[binding.REPORT]).is_file()
    lines = Path(command[-1]).read_text().splitlines()
    init_agent._started_marker(Path(command[-1])).touch()
    (line,) = [text for text in lines if " select-profile " in text]
    words = shlex.split(line)
    install.cmd_claude(words[words.index("--") + 1 :])


def _canary(report: Path, executed: list[Exec]) -> dict[str, Any]:
    """The validation the exec'd claude process writes: the binding the launch requested, on that process's account."""
    rendered = json.loads(report.read_text())["rendered"]
    account = account_sessions.account_from_names(executed[-1][2])
    return {**rendered, "state": "validated", "pid": os.getpid(), "account": account}


def _front_door(tmp_path: Path, executed: list[Exec]) -> tuple[Any, dict[str, str]]:
    """Spawn the front door master of swarm proof through the runtime with the pane run in process, and return the
    placement with the fields of the report init-agent printed."""
    home = tmp_path / "home"
    home.mkdir()
    reports: list[str] = []
    runtime = HerdrRuntime(
        home=home,
        run=lambda argv, **_: _pane(tmp_path, executed, argv, reports),
        choose=lambda *_: ("claude", "requested"),
    )
    config = SimpleNamespace(
        slug="proof",
        repo=str(home),
        code="a1b2c3",
        compact_limit=0,
        lanes={"master": {"agent": "claude"}},
        autonomy="delegate",
    )
    task = {"id": "master", "title": "proof", "route_kind": "interactive", "seat": "master@proof"}
    placed = runtime.spawn(config, "master", "agent", task)
    return placed, parse_fields(reports[-1])


@pytest.mark.feature("front-door-launch-chain")
def test_the_front_door_launch_execs_claude_on_the_login_slot_with_remote_control_and_no_token(
    tmp_path: Path, executed: list[Exec]
) -> None:
    placed, report = _front_door(tmp_path, executed)
    ((binary, argv, environ),) = executed
    assert argv[:4] == [binary, "--dangerously-skip-permissions", *login.SETTINGS]
    at = argv.index("--remote-control")
    assert argv[at : at + 4] == RC
    assert not [name for name in environ if name == "CLAUDE_CODE_OAUTH_TOKEN" or name.startswith("AH_CC_TOKEN_")]
    assert environ[MARK] == "loaded"
    assert environ["CLAUDE_CONFIG_DIR"] == str(login.home(1))
    assert (placed.route_kind, placed.account) == ("interactive", "oneatexample_com")
    assert placed.remote_control == {"state": "on", "detail": TITLE}
    assert (report["status"], report["account"], report["profile_validation"]) == (
        "started",
        "oneatexample_com",
        "validated",
    )
    assert report["route_status"] in init_agent.STARTED_ROUTES and report["placement"]
    assert json.loads(report["profile_binding"])["account"] == "oneatexample_com"


@pytest.mark.feature("front-door-launch-chain")
def test_a_primary_login_at_its_week_reserve_hands_the_front_door_launch_to_the_next_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, executed: list[Exec]
) -> None:
    monkeypatch.setattr(master_logins, "week_left", lambda account, now: 1.0 if account == "oneatexample_com" else 90.0)
    placed, _ = _front_door(tmp_path, executed)
    ((_, argv, environ),) = executed
    assert environ["CLAUDE_CONFIG_DIR"] == str(login.home(2))
    assert not [name for name in environ if name == "CLAUDE_CODE_OAUTH_TOKEN" or name.startswith("AH_CC_TOKEN_")]
    assert environ[MARK] == "loaded"
    at = argv.index("--remote-control")
    assert argv[at : at + 4] == RC
    assert (placed.route_kind, placed.account) == ("interactive", "twoatexample_com")
    assert placed.remote_control == {"state": "on", "detail": TITLE}


@pytest.mark.feature("front-door-launch-chain")
def test_an_expired_front_door_login_execs_the_token_fallback_and_reports_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, executed: list[Exec]
) -> None:
    from scripts import claude_quota_balancer as router

    def expired(*_: Any) -> dict[str, Any]:
        raise native_login.LoginError(native_login.EXPIRED)

    credential = router.Credential("AH_CC_TOKEN_workeratexample_com", "routed")
    monkeypatch.setattr(native_login, "check", expired)
    monkeypatch.setattr(account_sessions, "sessions_by_account", lambda: {})
    monkeypatch.setattr(
        router, "select_master_credential", lambda environ, **_: router.RouteDecision(credential, None, "cache")
    )
    placed, _ = _front_door(tmp_path, executed)
    ((_, argv, environ),) = executed
    assert not {"--remote-control", "--remote-control-session-name-prefix"} & set(argv)
    assert environ["CLAUDE_CODE_OAUTH_TOKEN"] == "routed"
    assert placed.account == "workeratexample_com"
    assert placed.remote_control == {"state": "off", "detail": remote_control.TOKEN}
    assert remote_control.line({"remote_control": placed.remote_control}) == (
        "Remote Control: off (subscription token fallback), phone access unavailable"
    )
