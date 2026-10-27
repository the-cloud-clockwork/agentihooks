import json
import os

import pytest

from scripts import claude_login, master_logins, openbao_kv, openbao_step
from scripts import master_claude_login as login

WORKER = "AH_CC_TOKEN_workeratexample_com"


@pytest.fixture(autouse=True)
def ah(tmp_path, monkeypatch):
    monkeypatch.setattr(login, "MANAGED", tmp_path / "no-managed-settings.json")
    monkeypatch.setattr(openbao_kv, "from_env", lambda environ: None)
    monkeypatch.setattr(login.hooks_config, "AGENTIHOOKS_HOME", tmp_path / "ah")
    monkeypatch.delenv(login.SLOT_ENV, raising=False)
    return tmp_path / "ah"


@pytest.fixture
def rendered(tmp_path):
    out = tmp_path / "profiles" / "master" / "claude"
    out.mkdir(parents=True)
    (out / "settings.json").write_text("{}")
    (out / login.CONFIG).write_text("{}")
    return out


def _stored(number, email):
    home = login.home(number)
    home.mkdir(parents=True)
    (home / login.CREDENTIALS).write_text(json.dumps({login.GRANT: {"refreshToken": email}}))
    (home / login.CONFIG).write_text(json.dumps({login.ACCOUNT: {"emailAddress": email}}))


def _environ(rendered, name):
    return {"AGENTIHOOKS_AGENT_NAME": name, "CLAUDE_CONFIG_DIR": str(rendered), WORKER: "x"}


def _launch_elsewhere(monkeypatch, rendered, name):
    with monkeypatch.context() as patch:
        patch.setattr(login.os, "getpid", os.getppid)
        return login.prepare(_environ(rendered, name), rendered.parent)


def _room(monkeypatch, **left):
    monkeypatch.setattr(master_logins, "left", lambda account, now: left[account] + 0 * now)


@pytest.mark.parametrize("number", [1, 2])
def test_two_live_front_doors_share_the_preferred_stored_login(monkeypatch, rendered, number):
    _stored(1, "one@example.com")
    _stored(2, "two@example.com")
    _room(monkeypatch, oneatexample_com=50.0 if number == 1 else 40.0, twoatexample_com=50.0 if number == 2 else 40.0)
    first, first_account = _launch_elsewhere(monkeypatch, rendered, "master@rig-1")
    (first / "live-session").write_text("keep")
    second, second_account = login.prepare(_environ(rendered, "master@doctor-1"), rendered.parent)
    assert first == login.home(number)
    assert first_account == ("oneatexample_com" if number == 1 else "twoatexample_com")
    assert (second, second_account) == (first, first_account)
    assert (second / "live-session").read_text() == "keep"
    assert login.holders(login.root(number)) == {"master@rig-1": os.getppid(), "master@doctor-1": os.getpid()}
    assert login.holders(login.root(3 - number)) == {}


def test_a_master_launches_on_the_stored_login_with_the_most_room(monkeypatch, rendered):
    for number, email in ((1, "one@example.com"), (2, "two@example.com"), (3, "three@example.com")):
        _stored(number, email)
    _room(monkeypatch, oneatexample_com=10.0, twoatexample_com=70.0, threeatexample_com=30.0)
    target, found = login.prepare(_environ(rendered, "master@rig-1"), rendered.parent)
    assert (target, found) == (login.home(2), "twoatexample_com")


def test_releasing_one_front_door_preserves_the_other_holder(monkeypatch, rendered):
    _stored(1, "one@example.com")
    first, _ = _launch_elsewhere(monkeypatch, rendered, "master@rig-1")
    second, _ = login.prepare(_environ(rendered, "master@doctor-1"), rendered.parent)
    login.release(second, _environ(rendered, "master@doctor-1"))
    assert first == second == login.home(1)
    assert login.holders(login.root(1)) == {"master@rig-1": os.getppid()}
    with pytest.raises(login.LoginRefused, match="master@rig-1"):
        login.claim(claude_login.PROG, os.getpid(), alone=True)


def test_free_lists_logins_no_other_live_master_holds(monkeypatch, rendered):
    _stored(1, "one@example.com")
    _stored(2, "two@example.com")
    _room(monkeypatch, oneatexample_com=50.0, twoatexample_com=40.0)
    _launch_elsewhere(monkeypatch, rendered, "master@rig-1")
    assert login.free("master@rig-1") == ["oneatexample_com", "twoatexample_com"]
    assert login.free("master@doctor-1") == ["twoatexample_com"]


def test_a_master_at_98_percent_of_its_week_moves_to_the_stored_login_with_most_room(monkeypatch, rendered):
    from hooks.context import quota_policy
    from hooks.context.quota_policy import Candidate

    _stored(1, "one@example.com")
    _stored(2, "two@example.com")
    _stored(3, "three@example.com")
    used = {"oneatexample_com": (40.0, 98.0), "twoatexample_com": (20.0, 60.0), "threeatexample_com": (5.0, 10.0)}
    monkeypatch.setattr(master_logins, "usage", lambda account, now: (*used[account], now))
    environ = _environ(rendered, "master@rig-1")
    token = Candidate("workeratexample_com", 0.0, 0.0, 0, 0.0, 6)
    others = quota_policy.master_targets([token, *quota_policy._login_accounts(environ)], environ)
    decision = quota_policy.decide(
        account="oneatexample_com",
        five_used=40.0,
        week_used=98.0,
        five_reset=None,
        week_reset=None,
        others=others,
        push=False,
    )
    assert decision.action == "handoff"
    assert (decision.target.account, decision.target.login) == ("threeatexample_com", True)
    assert "workeratexample_com" not in {c.account for c in decision.others}
    monkeypatch.setattr(master_logins, "left", lambda account, now: 100.0 - max(used[account][:2]))
    target, found = login.prepare(_environ(rendered, "master@rig-2"), rendered.parent)
    assert (target, found) == (login.home(3), "threeatexample_com")


def test_a_master_at_99_percent_of_five_hours_moves_to_the_stored_login_with_most_room(monkeypatch, rendered):
    from hooks.context import quota_policy
    from hooks.context.quota_policy import Candidate

    _stored(1, "one@example.com")
    _stored(2, "two@example.com")
    _stored(3, "three@example.com")
    used = {"oneatexample_com": (99.0, 50.0), "twoatexample_com": (60.0, 20.0), "threeatexample_com": (10.0, 30.0)}
    monkeypatch.setattr(master_logins, "usage", lambda account, now: (*used[account], now))
    environ = _environ(rendered, "master@rig-1")
    token = Candidate("workeratexample_com", 0.0, 0.0, 0, 0.0, 6)
    others = quota_policy.master_targets([token, *quota_policy._login_accounts(environ)], environ)
    decision = quota_policy.decide(
        account="oneatexample_com",
        five_used=99.0,
        week_used=50.0,
        five_reset=None,
        week_reset=None,
        others=others,
        push=False,
    )
    assert (decision.action, decision.trigger) == ("handoff", "five_hour")
    assert (decision.target.account, decision.target.login) == ("threeatexample_com", True)


def test_a_master_with_no_free_stored_login_gets_no_token_target(monkeypatch, rendered):
    from hooks.context import quota_policy
    from hooks.context.quota_policy import Candidate

    _stored(1, "one@example.com")
    monkeypatch.setattr(master_logins, "usage", lambda account, now: (40.0, 98.0, now))
    environ = _environ(rendered, "master@rig-1")
    token = Candidate("workeratexample_com", 0.0, 0.0, 0, 0.0, 6)
    others = quota_policy.master_targets([token, *quota_policy._login_accounts(environ)], environ)
    decision = quota_policy.decide(
        account="oneatexample_com",
        five_used=40.0,
        week_used=98.0,
        five_reset=None,
        week_reset=None,
        others=others,
        push=False,
    )
    assert decision.action == "stop"
    assert decision.target is None


def test_a_worker_gets_no_login_candidates():
    from hooks.context import quota_policy

    assert quota_policy._login_accounts({"AGENTIHOOKS_AGENT_NAME": "engineer@rig-1"}) == []


def test_a_new_login_takes_the_first_empty_slot_and_a_full_set_refuses(monkeypatch):
    _stored(1, "one@example.com")
    assert login.empty() == 2
    monkeypatch.setattr(login, "MAX_SLOTS", 2)
    _stored(2, "two@example.com")
    with pytest.raises(login.LoginRefused, match="^all 2 master login slots hold a login"):
        login.empty()


def test_listing_names_each_login_and_its_holder(monkeypatch, rendered, capsys):
    assert claude_login.main(["--list"], {}) == 0
    assert capsys.readouterr().out == "no master claude ai login is stored: run agentihooks claude login --master\n"
    _stored(1, "one@example.com")
    _stored(3, "three@example.com")
    _room(monkeypatch, oneatexample_com=50.0, threeatexample_com=40.0)
    _launch_elsewhere(monkeypatch, rendered, "master@rig-1")
    used = {"oneatexample_com": (12.4, 98.0), "threeatexample_com": (0.0, 3.6)}
    monkeypatch.setattr(master_logins, "usage", lambda account, now: (*used[account], 0 * now))
    assert claude_login.main(["--list"], {}) == 0
    assert capsys.readouterr().out == (
        "slot 1: oneatexample_com, master@rig-1, 5h 12% used, 7d 98% used\n"
        "slot 3: threeatexample_com, free, 5h 0% used, 7d 4% used\n"
    )


def _console():
    return openbao_step.Console(lambda prompt: "", lambda prompt: "", lambda: False)


def test_removing_a_free_login_clears_its_home_and_record(monkeypatch, capsys):
    _stored(2, "two@example.com")
    merged = []
    monkeypatch.setattr(openbao_step, "target", lambda *_: "bao")
    monkeypatch.setattr(openbao_kv, "merge", lambda bao, path, entries, stale=(): merged.append((path, stale)))
    assert claude_login.main(["--remove", "2"], {}, console=_console()) == 0
    assert capsys.readouterr().out == "master login slot 2 (twoatexample_com) removed\n"
    assert not login.home(2).exists()
    assert merged == [("agentihooks/master-claude-login-2", login.RECORD_KEYS)]


def test_removing_a_held_or_empty_login_is_refused(monkeypatch, rendered, capsys):
    monkeypatch.setattr(openbao_step, "target", lambda *_: None)
    assert claude_login.main(["--remove", "2"], {}, console=_console()) == 1
    assert "master login slot 2 holds no login" in capsys.readouterr().err
    _stored(1, "one@example.com")
    _launch_elsewhere(monkeypatch, rendered, "master@rig-1")
    assert claude_login.main(["--remove", "1"], {}, console=_console()) == 1
    assert "held by master@rig-1, still running" in capsys.readouterr().err
    assert login.home(1).exists()
    assert claude_login.main(["--remove", str(login.MAX_SLOTS + 1)], {}, console=_console()) == 1
    assert f"outside 1 to {login.MAX_SLOTS}" in capsys.readouterr().err
