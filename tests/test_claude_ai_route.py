import json
import os
import time
from pathlib import Path

import pytest

from scripts import master_claude_login as login
from scripts import master_logins, openbao_kv, quota_state
from scripts.profiles import binding
from scripts.routing import claude_ai, native_login

PREFERRED = {"emailAddress": "nestor.colt@gmail.com"}
SECOND = {"emailAddress": "thecloudclockwork@gmail.com"}
GRANT = {"accessToken": "access", "refreshToken": "refresh", "expiresAt": 2000}
ORDER = "nestor.colt@gmail.com,thecloudclockwork@gmail.com"


@pytest.fixture(autouse=True)
def ah(tmp_path, monkeypatch):
    monkeypatch.setattr(login, "MANAGED", tmp_path / "no-managed-settings.json")
    monkeypatch.setattr(login.hooks_config, "AGENTIHOOKS_HOME", str(tmp_path / "ah"))
    monkeypatch.setattr(openbao_kv, "from_env", lambda environ: None)
    monkeypatch.setenv(login.ORDER_ENV, ORDER)
    monkeypatch.chdir(tmp_path)
    return tmp_path / "ah"


@pytest.fixture
def rendered(tmp_path):
    out = tmp_path / "profiles" / "engineer" / "claude"
    out.mkdir(parents=True)
    (out / "settings.json").write_text("{}")
    (out / "CLAUDE.md").write_text("engineer persona")
    (out / ".claude.json").write_text(json.dumps({"mcpServers": {}}))
    (out / ".credentials.json").symlink_to(tmp_path / "operator-credentials.json")
    return out


@pytest.fixture
def checked(monkeypatch):
    seen = []

    def check(environ, account, run=None):
        seen.append((account, environ[claude_ai.STORE], environ["CLAUDE_CONFIG_DIR"]))
        return dict(native_login.PROOF)

    monkeypatch.setattr(native_login, "check", check)
    return seen


@pytest.fixture
def executed(monkeypatch):
    calls = []
    monkeypatch.setattr(claude_ai.os, "execvpe", lambda binary, argv, env: calls.append((binary, argv, env)))
    return calls


def _slot(number, account, grant=GRANT):
    home = login.home(number)
    home.mkdir(parents=True)
    (home / login.CREDENTIALS).write_text(json.dumps({login.GRANT: grant}))
    (home / login.CONFIG).write_text(json.dumps({login.ACCOUNT: account}))
    return home


def _left(values):
    def left(account, now):
        assert abs(now - time.time()) < 60
        return values[account]

    return left


def _environ(rendered, **extra):
    return {"CLAUDE_CONFIG_DIR": str(rendered), "AGENTIHOOKS_AGENT_NAME": "s-251025", **extra}


def _master_holds(number):
    folder = login.root(number)
    (folder / login.OWNER).write_text(
        json.dumps({"holders": {"master@rig": os.getpid()}, "epoch": 3, "alone": "master@rig"})
    )
    return (folder / login.OWNER).read_bytes()


def test_the_launcher_table_routes_claude_ai_to_the_standalone_launcher():
    from scripts import install

    assert install.ROUTE_LAUNCHERS["claude-ai"] == "scripts.routing.claude_ai"
    assert claude_ai.ROUTE == "claude-ai"


def test_the_preferred_login_wins_over_the_one_with_more_room(rendered, monkeypatch, checked, executed):
    _slot(1, SECOND)
    _slot(2, PREFERRED)
    monkeypatch.setattr(
        master_logins, "left", _left({"thecloudclockworkatgmail_com": 90.0, "nestor_coltatgmail_com": 40.0})
    )
    claude_ai.launch_claude("/opt/claude", [], _environ(rendered), "")
    assert [account for account, _, _ in checked] == ["nestor_coltatgmail_com"]
    assert executed[0][2][claude_ai.STORE] == str(login.home(2))
    home = executed[0][2]["CLAUDE_CONFIG_DIR"]
    assert json.loads((Path(home) / login.CONFIG).read_text())[login.ACCOUNT] == PREFERRED


def test_a_spent_login_falls_to_the_next_in_order(rendered, monkeypatch, checked, executed, capsys):
    _slot(1, PREFERRED)
    _slot(2, SECOND)
    monkeypatch.setattr(
        master_logins,
        "left",
        _left({"nestor_coltatgmail_com": float(quota_state.DRAIN_LEFT), "thecloudclockworkatgmail_com": 10.5}),
    )
    claude_ai.launch_claude("/opt/claude", [], _environ(rendered), "")
    assert [account for account, _, _ in checked] == ["thecloudclockworkatgmail_com"]
    assert executed[0][2][claude_ai.STORE] == str(login.home(2))
    assert capsys.readouterr().out == "[agenti] account=thecloudclockworkatgmail_com route=claude-ai slot=2\n"


def test_a_login_that_fails_its_native_check_falls_to_the_next(rendered, monkeypatch, executed):
    _slot(1, PREFERRED)
    _slot(2, SECOND)
    monkeypatch.setattr(master_logins, "left", lambda account, now: 80.0)
    tried = []

    def check(environ, account, run=None):
        tried.append(account)
        if account == "nestor_coltatgmail_com":
            raise native_login.LoginError(native_login.EXPIRED)
        return dict(native_login.PROOF)

    monkeypatch.setattr(native_login, "check", check)
    claude_ai.launch_claude("/opt/claude", [], _environ(rendered), "")
    assert tried == ["nestor_coltatgmail_com", "thecloudclockworkatgmail_com"]
    assert executed[0][2][claude_ai.STORE] == str(login.home(2))


class _Stdout:
    def __init__(self):
        self.text, self.flushed = "", ""

    def write(self, text):
        self.text += text

    def flush(self):
        self.flushed = self.text


def test_the_session_shares_a_held_master_slot_without_claiming_it(rendered, monkeypatch, checked, executed, tmp_path):
    from scripts import install

    _slot(1, PREFERRED)
    held = _master_holds(1)
    monkeypatch.setattr(master_logins, "left", lambda account, now: 50.0)
    report, slots, out = tmp_path / "route", [], _Stdout()

    def execute(harness, account, environ, invoke):
        slots.append((harness, account, out.flushed))
        return invoke(environ)

    monkeypatch.setattr("scripts.hive.account_slots.execute", execute)
    monkeypatch.setattr("sys.stdout", out)
    claude_ai.launch_claude(
        "/opt/claude", ["--model", "opus"], _environ(rendered, CLAUDE_CODE_OAUTH_TOKEN="x"), str(report)
    )
    binary, argv, env = executed[0]
    assert slots == [
        ("claude", "nestor_coltatgmail_com", "[agenti] account=nestor_coltatgmail_com route=claude-ai slot=1\n")
    ]
    target = login.root(1).parent / "claude-ai" / "s_251025" / "claude"
    assert (login.root(1) / login.OWNER).read_bytes() == held
    assert login.holders(login.root(1)) == {"master@rig": os.getpid()}
    assert env["CLAUDE_CONFIG_DIR"] == str(target)
    assert env[claude_ai.STORE] == str(login.home(1))
    assert not (target / login.CREDENTIALS).exists()
    assert json.loads((target / login.CONFIG).read_text()) == {"mcpServers": {}, login.ACCOUNT: PREFERRED}
    assert (target / "CLAUDE.md").read_text() == "engineer persona"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert env["AH_ROUTE_INTERACTIVE_nestor_coltatgmail_com"] == "1"
    assert binary == "/opt/claude"
    assert argv == install._claude_command(
        "/opt/claude", ["--settings", '{"forceLoginMethod":"claudeai"}', "--model", "opus"]
    )
    assert report.read_text().splitlines() == ["status=routed", "account=nestor_coltatgmail_com", "placement=forced"]


def test_a_master_claim_after_a_standalone_launch_takes_the_slot_alone(rendered, monkeypatch, checked, executed):
    _slot(1, PREFERRED)
    monkeypatch.setattr(master_logins, "left", lambda account, now: 50.0)
    claude_ai.launch_claude("/opt/claude", [], _environ(rendered), "")
    assert login.holders(login.root(1)) == {}
    assert login.claim("master@rig", os.getpid(), floor=0, alone=True) == 1


def test_a_stored_copy_restores_into_a_free_slot_home(rendered, monkeypatch, checked, executed):
    copy = {"grant": json.dumps(GRANT), "account": json.dumps(PREFERRED), "epoch": "2"}
    monkeypatch.setattr(openbao_kv, "from_env", lambda environ: "bao" if environ.get("OPENBAO_URL") else None)
    monkeypatch.setattr(login, "stored", lambda bao, number=None: copy if (bao, number) == ("bao", 3) else {})
    monkeypatch.setattr(master_logins, "left", lambda account, now: 50.0)
    claude_ai.launch_claude("/opt/claude", [], _environ(rendered, OPENBAO_URL="u"), "")
    assert json.loads((login.home(3) / login.CREDENTIALS).read_text()) == {login.GRANT: GRANT}
    assert executed[0][2][claude_ai.STORE] == str(login.home(3))
    assert checked == [("nestor_coltatgmail_com", str(login.home(3)), executed[0][2]["CLAUDE_CONFIG_DIR"])]
    assert login.home(3).stat().st_mode & 0o777 == login.PRIVATE_DIR


def test_a_stored_copy_restores_into_an_empty_slot_folder(rendered, monkeypatch, checked, executed):
    copy = {"grant": json.dumps(GRANT), "account": json.dumps(PREFERRED), "epoch": "2"}
    login.home(1).mkdir(parents=True)
    monkeypatch.setattr(openbao_kv, "from_env", lambda environ: "bao")
    monkeypatch.setattr(login, "stored", lambda bao, number=None: copy if number == 1 else {})
    monkeypatch.setattr(master_logins, "left", lambda account, now: 50.0)
    claude_ai.launch_claude("/opt/claude", [], _environ(rendered), "")
    assert json.loads((login.home(1) / login.CREDENTIALS).read_text()) == {login.GRANT: GRANT}


def test_a_stored_copy_never_restores_into_a_slot_a_master_holds(rendered, monkeypatch, checked, executed):
    copy = {"grant": json.dumps(GRANT), "account": json.dumps(PREFERRED), "epoch": "2"}
    login.root(1).mkdir(parents=True)
    _master_holds(1)
    monkeypatch.setattr(openbao_kv, "from_env", lambda environ: "bao")
    monkeypatch.setattr(login, "stored", lambda bao, number=None: copy if number == 1 else {})
    monkeypatch.setattr(master_logins, "left", lambda account, now: 50.0)
    claude_ai.launch_claude("/opt/claude", [], _environ(rendered), "")
    assert not (login.home(1) / login.CREDENTIALS).exists()


def test_unranked_logins_follow_the_ranked_ones_by_slot_number(rendered, monkeypatch, checked, executed):
    _slot(1, {"emailAddress": "zed@example.com"})
    _slot(2, {"emailAddress": "amy@example.com"})
    _slot(3, SECOND)
    monkeypatch.setattr(master_logins, "left", lambda account, now: 50.0)
    claude_ai.launch_claude("/opt/claude", [], _environ(rendered), "")
    monkeypatch.delenv(login.ORDER_ENV)
    claude_ai.launch_claude("/opt/claude", [], _environ(rendered), "")
    assert [account for account, _, _ in checked] == ["thecloudclockworkatgmail_com", "zedatexample_com"]


def test_no_usable_login_refuses_with_every_reason_and_never_routes_a_token(rendered, monkeypatch, executed):
    from scripts.claude_quota_balancer import RoutingError

    _slot(1, PREFERRED)
    _slot(2, SECOND)
    monkeypatch.setattr(
        master_logins, "left", _left({"nestor_coltatgmail_com": 3.0, "thecloudclockworkatgmail_com": 60.0})
    )

    def check(environ, account, run=None):
        raise native_login.LoginError(native_login.EXPIRED)

    monkeypatch.setattr(native_login, "check", check)
    with pytest.raises(RoutingError) as refused:
        claude_ai.launch_claude("/opt/claude", [], _environ(rendered, AH_CC_TOKEN_workeratexample_com="x"), "")
    assert str(refused.value) == (
        "no usable claude ai login: nestor_coltatgmail_com has 3% left; "
        f"thecloudclockworkatgmail_com: {native_login.EXPIRED}"
    )
    assert executed == []


def test_no_stored_login_refuses_naming_the_login_command(rendered, executed):
    from scripts.claude_quota_balancer import RoutingError

    with pytest.raises(RoutingError, match="^no claude ai login is stored: run agentihooks claude login --master$"):
        claude_ai.launch_claude("/opt/claude", [], _environ(rendered), "")
    assert executed == []


def test_settings_that_bring_back_a_credential_refuse_before_any_login_is_read(rendered, monkeypatch, executed):
    from scripts.claude_quota_balancer import RoutingError

    _slot(1, PREFERRED)
    (rendered / "settings.json").write_text('{"apiKeyHelper": "/bin/key"}')
    monkeypatch.setattr(master_logins, "left", lambda account, now: pytest.fail("quota read before refusal"))
    with pytest.raises(RoutingError, match="sets apiKeyHelper$"):
        claude_ai.launch_claude("/opt/claude", [], _environ(rendered), "")


def test_a_launch_without_an_agent_name_builds_a_home_by_process(rendered, monkeypatch, checked, executed):
    _slot(1, PREFERRED)
    monkeypatch.setattr(master_logins, "left", lambda account, now: 50.0)
    claude_ai.launch_claude("/opt/claude", [], {"CLAUDE_CONFIG_DIR": str(rendered)}, "")
    assert executed[0][2]["CLAUDE_CONFIG_DIR"] == str(
        login.root(1).parent / "claude-ai" / f"pid_{os.getpid()}" / "claude"
    )


def test_the_launch_report_is_rehomed_to_the_standalone_home(rendered, monkeypatch, checked, executed):
    _slot(1, PREFERRED)
    monkeypatch.setattr(master_logins, "left", lambda account, now: 50.0)
    seen = []
    monkeypatch.setattr(binding, "rehome", lambda report, home, shared=False: seen.append((str(report), home, shared)))
    claude_ai.launch_claude("/opt/claude", [], _environ(rendered, AGENTIHOOKS_PROFILE_REPORT="/run/report"), "")
    assert seen == [("/run/report", login.root(1).parent / "claude-ai" / "s_251025" / "claude", False)]


def test_the_claude_launcher_hands_the_route_over_without_holding_the_route_lock(tmp_path, monkeypatch):
    import fcntl

    from scripts import install

    seen = []
    monkeypatch.setattr(install, "_load_claude_runtime_env", lambda: None)
    monkeypatch.setattr("scripts.deps_preflight.ensure", lambda: None)
    monkeypatch.setattr("scripts.claude_quota_balancer._cache_path", lambda _: tmp_path / "quota.json")
    monkeypatch.setattr(install.shutil, "which", lambda name: f"/opt/{name}")

    def launch(*args):
        with (tmp_path / "claude-route.lock").open("a+") as waiting:
            fcntl.flock(waiting, fcntl.LOCK_EX | fcntl.LOCK_NB)
        seen.append(args)

    monkeypatch.setattr(claude_ai, "launch_claude", launch)
    install.cmd_claude(["--route", "claude-ai", "--agentihooks-fallback-bare", "--version"])
    assert seen == [("/opt/claude", ["--version"], install.os.environ, "")]


def test_the_interactive_route_still_launches_under_the_route_lock(tmp_path, monkeypatch):
    import fcntl

    from scripts import install
    from scripts.routing import interactive

    seen = []
    monkeypatch.setattr(install, "_load_claude_runtime_env", lambda: None)
    monkeypatch.setattr("scripts.deps_preflight.ensure", lambda: None)
    monkeypatch.setattr("scripts.claude_quota_balancer._cache_path", lambda _: tmp_path / "quota.json")
    monkeypatch.setattr(install.shutil, "which", lambda name: f"/opt/{name}")

    def launch(*args):
        with (tmp_path / "claude-route.lock").open("a+") as waiting, pytest.raises(BlockingIOError):
            fcntl.flock(waiting, fcntl.LOCK_EX | fcntl.LOCK_NB)
        seen.append(args)

    monkeypatch.setattr(interactive, "launch_claude", launch)
    install.cmd_claude(["--route", "interactive", "--version"])
    assert seen == [("/opt/claude", ["--version"], install.os.environ, "")]
