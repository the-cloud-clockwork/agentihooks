from dataclasses import replace

import pytest

from scripts import master_claude_login
from scripts.swarm.capacity import Account
from tests.swarm.test_master_auth import _login_runtime

pytestmark = pytest.mark.unit

LAUNCH = {"profile": "master", "harness": "claude", "account": "login", "model": "opus", "effort": "high"}


def _accounts(login_week_left):
    return [
        Account("claude", "login", "OPEN", 1, 97.0, login_week_left, 6),
        Account("claude", "small", "OPEN", 0, 90.0, 30.0, 6),
        Account("claude", "roomy", "OPEN", 0, 90.0, 80.0, 6),
    ]


@pytest.fixture
def login(monkeypatch):
    lanes = []

    def enabled(slug, seat, lane, harness):
        assert (slug, seat) == ("proof", "")
        lanes.append((lane, harness))
        return lane == "master" and harness == "claude"

    monkeypatch.setattr(master_claude_login, "enabled", enabled)
    monkeypatch.setattr(master_claude_login, "login_account", lambda: "login")
    return lanes


def _spawn(tmp_path, login_week_left, reason=""):
    runtime, config, seen = _login_runtime(tmp_path, _accounts(login_week_left))
    runtime.choose = lambda *_: ("claude", "requested")
    config.lanes = {"master": {"agent": "claude"}}
    envelope = {"launch": LAUNCH, **({"reason": reason} if reason else {})}
    task = {"id": "master", "title": "proof", "handoff": "Continue the master task.", "handoff_envelope": envelope}
    placed = runtime.spawn(config, "master", "agent", task)
    return placed, seen[0][seen[0].index("--") + 1 :][:2]


def test_a_quota_handoff_moves_the_master_off_its_login_to_the_account_with_the_most_room(tmp_path, login):
    placed, route = _spawn(tmp_path, 2.0, "quota")
    assert (placed.route_kind, route) == ("subscription", ["--route", "roomy"])


def test_a_master_recycled_while_its_login_is_spent_stays_off_the_login(tmp_path, login):
    placed, route = _spawn(tmp_path, 2.0)
    assert placed.route_kind == "subscription"
    assert route[0] == "--route" and route[1] in ("small", "roomy")


def test_a_master_returns_to_its_login_while_it_has_room(tmp_path, login):
    placed, route = _spawn(tmp_path, 50.0)
    assert (placed.route_kind, route) == ("interactive", ["--route", master_claude_login.ROUTE])


def test_the_five_hour_window_spends_the_login_too(tmp_path, monkeypatch, login):
    runtime, _, _ = _login_runtime(tmp_path, [replace(_accounts(50.0)[0], five_left=1.0)])
    assert runtime._login_spent()
    runtime._quota_accounts = [replace(_accounts(50.0)[0], five_left=1.5)]
    assert not runtime._login_spent()
    runtime._quota_accounts = [replace(_accounts(2.0)[0], name="other")]
    assert not runtime._login_spent()
    runtime._quota_accounts = [replace(_accounts(2.0)[0], harness="codex")]
    assert not runtime._login_spent()


def test_a_reserved_login_is_judged_on_its_own_quota(tmp_path, login):
    runtime, _, _ = _login_runtime(tmp_path, [replace(_accounts(50.0)[0], reserved="reserved for 1 live master")])
    assert not runtime._login_spent()


def test_the_login_account_is_the_slug_of_the_master_login_email(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(master_claude_login, "home", lambda: tmp_path)
    assert master_claude_login.login_account() == ""
    config = tmp_path / master_claude_login.CONFIG
    config.write_text(json.dumps({"oauthAccount": None}))
    assert master_claude_login.login_account() == ""
    config.write_text(json.dumps({"oauthAccount": {"emailAddress": ""}}))
    assert master_claude_login.login_account() == ""
    config.write_text(json.dumps({"oauthAccount": {"emailAddress": "nestor.colt@gmail.com"}}))
    assert master_claude_login.login_account() == "nestor_coltatgmail_com"


@pytest.mark.parametrize(
    ("login_week_left", "route"), [(2.0, ["--route", "roomy"]), (50.0, ["--route", master_claude_login.ROUTE])]
)
def test_a_moved_master_resumes_on_its_account_only_while_the_login_is_spent(tmp_path, login, login_week_left, route):
    from tests.swarm.test_runtime import _resuming

    runtime, config, agent, seen = _resuming(tmp_path, "c0ffee")
    runtime._quota_accounts = _accounts(login_week_left)
    config.slug = "proof"
    master = replace(agent, lane="master", task="master", seat="", account="roomy", route_kind="subscription")
    runtime.resume(config, master, "resume")
    argv = seen["runs"][0]
    assert argv[argv.index("--") + 1 :][:2] == route


class _Chat:
    def __init__(self):
        self.lines = []

    def say(self, slug, text, by=None):
        self.lines.append((slug, text, by))


def _moved(previous, account, client):
    from scripts.swarm import master_move
    from scripts.swarm.store import AgentRecord

    task = {"handoff_envelope": {"launch": {**LAUNCH, "account": previous}}} if previous is not None else {}
    record = AgentRecord("master@a1b2c3-0002", "master", "master", harness="claude", account=account)
    return master_move.account_moved("proof", task, record, client)


def test_a_master_that_lands_on_another_account_names_it_in_ledger_chat():
    chat = _Chat()
    assert _moved("login", "roomy", chat) == ["master master@a1b2c3-0002 moved from account login to roomy"]
    assert chat.lines == [
        (
            "proof",
            "The master now runs on claude account roomy; it moved from account login. "
            "Open that account on claude.ai to reach it.",
            None,
        )
    ]


@pytest.mark.parametrize(("previous", "account"), [("roomy", "roomy"), ("", "roomy"), (None, "roomy"), ("login", "")])
def test_a_master_that_keeps_its_account_posts_nothing(previous, account):
    chat = _Chat()
    assert _moved(previous, account, chat) == []
    assert chat.lines == []


def test_the_tick_names_the_new_account_when_a_master_successor_moves(monkeypatch):
    from scripts.swarm import ledger_client
    from scripts.swarm.store import MASTER
    from scripts.swarm.tick import Placed, _master
    from tests.swarm.test_tick_master import direct

    chat = _Chat()
    monkeypatch.setattr(ledger_client, "LedgerClient", lambda: chat)
    swarm, runtime = direct()
    spawn = runtime.spawn

    def moved(config, lane, name, task):
        placed = spawn(config, lane, name, task)
        return Placed(placed.pane_id, placed.harness, account="roomy")

    runtime.spawn = moved
    swarm.put_handoff("sw", MASTER, "Continue the master task.", envelope={"launch": {**LAUNCH, "account": "login"}})
    actions = _master("sw", swarm.config("sw"), swarm, runtime, 1)
    name = runtime.masters[0][0]
    assert actions == [f"spawned master {name}", f"master {name} moved from account login to roomy"]
    assert [(slug, by) for slug, _, by in chat.lines] == [("sw", None)]
    assert "account roomy" in chat.lines[0][1]
