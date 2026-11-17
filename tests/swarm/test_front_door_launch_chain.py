"""The front door master launch in its deployed shape: the swarm runtime's spawn, the init-agent launcher script, the
agentihooks claude entry and the claude ai login route run unstubbed between the login check and the process exec."""

import json
import os
import shlex
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import herdr_host, init_agent, install, master_logins, openbao_kv, select_profile
from scripts import master_claude_login as login
from scripts.profiles import binding
from scripts.routing import native_login
from scripts.swarm import remote_control
from scripts.swarm.runtime import HerdrRuntime
from tests.swarm.profile_fixture import validated

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
Exec = tuple[str, list[str], dict[str, str]]


@pytest.fixture
def executed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[Exec]:
    """Two stored claude ai logins, a pane environment holding worker tokens, and every process boundary recorded."""
    from scripts import claude_quota_balancer, deps_preflight, profile_telemetry
    from scripts.hive import account_slots

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
    for number, email in ((1, "one@example.com"), (2, "two@example.com")):
        target = login.home(number)
        target.mkdir(parents=True)
        (target / login.CREDENTIALS).write_text(json.dumps({login.GRANT: {"refreshToken": "fixture"}}))
        (target / login.CONFIG).write_text(json.dumps({login.ACCOUNT: {"emailAddress": email}}))
        (target.parent / login.OWNER).write_text(json.dumps({"holders": {"master@old": 999999999}, "epoch": 3}))
    for name, value in {**PANE, "CLAUDE_CONFIG_DIR": str(rendered)}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(binding.REPORT, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(herdr_host, "binary", lambda: None)
    monkeypatch.setattr(profile_telemetry, "installed_langfuse_env", lambda target: {})
    monkeypatch.setattr(select_profile, "prepare", lambda *args: ({"AGENTIHOOKS_PROFILE": "master"}, args[4]))
    monkeypatch.setattr(init_agent, "_launch_command", lambda launcher, *_: ("linux", ["terminal", str(launcher)]))
    monkeypatch.setattr(install.shutil, "which", lambda name: f"/opt/{name}")
    monkeypatch.setattr(install, "_load_claude_runtime_env", lambda: None)
    monkeypatch.setattr(deps_preflight, "ensure", lambda: None)
    monkeypatch.setattr(claude_quota_balancer, "_cache_path", lambda _: tmp_path / "quota.json")
    monkeypatch.setattr(account_slots, "execute", lambda harness, account, environ, invoke, **_: invoke(environ))
    seen: list[Exec] = []
    monkeypatch.setattr(os, "execvpe", lambda binary, argv, environ: seen.append((binary, argv, environ)))
    return seen


def _pane(tmp_path: Path, argv: list[str], **_: Any) -> SimpleNamespace:
    """Run what the herdr pane runs for an init-agent launch and print the lines init-agent reports back."""
    args = argv[argv.index("init-agent") + 1 :]
    at = args.index("--host")
    del args[at : at + 2]
    folder = tmp_path / "runtime"
    assert init_agent.main(["--dry-run", *args], {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(folder)}) == 0
    (launcher,) = folder.rglob("*.sh")
    (line,) = [text for text in launcher.read_text().splitlines() if " select-profile " in text]
    words = shlex.split(line)
    claude_args = words[words.index("--") + 1 :]
    install.cmd_claude(claude_args)
    route = init_agent._take_route_report(Path(claude_args[claude_args.index("--agentihooks-report") + 1]))[0]
    lines = [
        "status=started",
        f"route_status={route['status']}",
        f"account={route['account']}",
        f"placement={route['placement']}",
    ]
    return SimpleNamespace(returncode=0, stdout=validated(argv, "\n".join(lines) + "\n"), stderr="")


def _front_door(tmp_path: Path) -> Any:
    """Spawn the front door master of swarm proof through the runtime with the pane run in process."""
    home = tmp_path / "home"
    home.mkdir()
    runtime = HerdrRuntime(
        home=home, run=lambda argv, **kwargs: _pane(tmp_path, argv, **kwargs), choose=lambda *_: ("claude", "requested")
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
    return runtime.spawn(config, "master", "agent", task)


@pytest.mark.feature("front-door-launch-chain")
def test_the_front_door_launch_execs_claude_on_the_login_slot_with_remote_control_and_no_token(
    tmp_path: Path, executed: list[Exec]
) -> None:
    placed = _front_door(tmp_path)
    ((binary, argv, environ),) = executed
    assert argv[:4] == [binary, "--dangerously-skip-permissions", *login.SETTINGS]
    at = argv.index("--remote-control")
    assert argv[at : at + 4] == RC
    assert not [name for name in environ if name == "CLAUDE_CODE_OAUTH_TOKEN" or name.startswith("AH_CC_TOKEN_")]
    assert environ["CLAUDE_CONFIG_DIR"] == str(login.home(1))
    assert (placed.route_kind, placed.account) == ("interactive", "oneatexample_com")
    assert placed.remote_control == {"state": "on", "detail": TITLE}


@pytest.mark.feature("front-door-launch-chain")
def test_a_primary_login_at_its_week_reserve_hands_the_front_door_launch_to_the_next_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, executed: list[Exec]
) -> None:
    monkeypatch.setattr(master_logins, "week_left", lambda account, now: 1.0 if account == "oneatexample_com" else 90.0)
    placed = _front_door(tmp_path)
    ((_, argv, environ),) = executed
    assert environ["CLAUDE_CONFIG_DIR"] == str(login.home(2))
    assert not [name for name in environ if name == "CLAUDE_CODE_OAUTH_TOKEN" or name.startswith("AH_CC_TOKEN_")]
    at = argv.index("--remote-control")
    assert argv[at : at + 4] == RC
    assert (placed.route_kind, placed.account) == ("interactive", "twoatexample_com")
    assert placed.remote_control == {"state": "on", "detail": TITLE}


@pytest.mark.feature("front-door-launch-chain")
def test_an_expired_front_door_login_execs_the_token_fallback_and_reports_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, executed: list[Exec]
) -> None:
    from hooks.context import account_sessions
    from scripts import claude_quota_balancer as router

    def expired(*_: Any) -> dict[str, Any]:
        raise native_login.LoginError(native_login.EXPIRED)

    credential = router.Credential("AH_CC_TOKEN_workeratexample_com", "routed")
    monkeypatch.setattr(native_login, "check", expired)
    monkeypatch.setattr(account_sessions, "sessions_by_account", lambda: {})
    monkeypatch.setattr(
        router, "select_master_credential", lambda environ, **_: router.RouteDecision(credential, None, "cache")
    )
    placed = _front_door(tmp_path)
    ((_, argv, environ),) = executed
    assert not {"--remote-control", "--remote-control-session-name-prefix"} & set(argv)
    assert environ["CLAUDE_CODE_OAUTH_TOKEN"] == "routed"
    assert placed.account == "workeratexample_com"
    assert placed.remote_control == {"state": "off", "detail": remote_control.TOKEN}
    assert remote_control.line({"remote_control": placed.remote_control}) == (
        "Remote Control: off (subscription token fallback), phone access unavailable"
    )
