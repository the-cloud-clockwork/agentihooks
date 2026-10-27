import json
import os
from types import SimpleNamespace

import pytest

from scripts import claude_login, master_logins, openbao_kv, openbao_step
from scripts import master_claude_login as login
from scripts.routing import reservation
from scripts.routing.settings import FileSettings

GRANT = {"refreshToken": "slot two", "expiresAt": 7}
TWO = {"emailAddress": "two@example.com"}


@pytest.fixture(autouse=True)
def ah(tmp_path, monkeypatch):
    monkeypatch.setattr(login, "MANAGED", tmp_path / "no-managed-settings.json")
    monkeypatch.setattr(login.hooks_config, "AGENTIHOOKS_HOME", str(tmp_path / "ah"))
    monkeypatch.delenv(login.SLOT_ENV, raising=False)
    return tmp_path / "ah"


@pytest.fixture
def rendered(tmp_path):
    out = tmp_path / "profiles" / "master" / "claude"
    out.mkdir(parents=True)
    (out / "settings.json").write_text("{}")
    (out / login.CONFIG).write_text("{}")
    return out


class Bao:
    def __init__(self, monkeypatch, records):
        self.records, self.merges = records, []
        monkeypatch.setattr(openbao_kv, "from_env", lambda environ: "bao")
        monkeypatch.setattr(openbao_kv, "read", self.read)
        monkeypatch.setattr(openbao_kv, "merge", self.merge)

    def read(self, bao, path):
        assert bao == "bao"
        return dict(self.records.get(path, {})), 1

    def merge(self, bao, path, entries, stale=()):
        assert bao == "bao"
        self.merges.append((path, dict(entries), tuple(stale)))


def _stored(number, account=None, grant=None):
    home = login.home(number)
    home.mkdir(parents=True)
    (home / login.CREDENTIALS).write_text(json.dumps({login.GRANT: grant or {"refreshToken": str(number)}}))
    (home / login.CONFIG).write_text(json.dumps({login.ACCOUNT: account} if account else {}))


def _held(number, *names):
    for name in names:
        login._claim(login.root(number), name, os.getppid(), login._alive, 0)


def _room(monkeypatch, **left):
    monkeypatch.setattr(master_logins, "left", lambda account, now: left[account] + 0 * now)


def test_the_slots_run_from_one_to_the_cap(monkeypatch):
    assert list(login.numbers()) == list(range(1, login.MAX_SLOTS + 1))
    monkeypatch.setattr(login, "MAX_SLOTS", 3)
    assert list(login.numbers()) == [1, 2, 3]


def test_a_login_is_named_from_its_home_then_its_stored_copy():
    assert login.login_account(2, {"account": json.dumps(TWO)}) == "twoatexample_com"
    assert login.login_account(2) == ""
    _stored(2, {"emailAddress": "home@example.com"})
    assert login.login_account(2, {"account": json.dumps(TWO)}) == "homeatexample_com"


def test_a_second_slot_reads_and_writes_only_its_own_record(monkeypatch):
    bao = Bao(
        monkeypatch, {"agentihooks/master-claude-login": {"epoch": "99"}, "agentihooks/master-claude-login-2": {}}
    )
    _stored(2, TWO, GRANT)
    (login.root(2) / login.OWNER).write_text(json.dumps({"epoch": 3}))
    (login.root(1)).mkdir(parents=True, exist_ok=True)
    (login.root(1) / login.OWNER).write_text(json.dumps({"epoch": 50}))
    assert login.stored("bao", 2) == {}
    assert login.checkpoint({"CLAUDE_CONFIG_DIR": str(login.home(2))}) is True
    entries = {"grant": json.dumps(GRANT), "account": json.dumps(TWO), "epoch": "3", "generation": "7"}
    assert bao.merges == [("agentihooks/master-claude-login-2", entries, ())]


def test_a_master_takes_a_login_only_its_stored_copy_holds(monkeypatch, rendered, tmp_path):
    copy = {"grant": json.dumps(GRANT), "account": json.dumps(TWO), "epoch": "4"}
    bao = Bao(monkeypatch, {"agentihooks/master-claude-login-2": copy})
    _stored(1, {"emailAddress": "one@example.com"})
    _room(monkeypatch, oneatexample_com=10.0, twoatexample_com=80.0)
    store = FileSettings(tmp_path / "routing-settings.json")
    monkeypatch.setattr(login, "routing_store", lambda environ: store)
    environ = {
        "AGENTIHOOKS_AGENT_NAME": "master@rig-1",
        "CLAUDE_CONFIG_DIR": str(rendered),
        "AH_CC_TOKEN_twoatexample_com": "x",
    }
    target, found = login.prepare(environ, tmp_path)
    assert (target, found) == (login.home(2), "twoatexample_com")
    assert json.loads((target / login.CREDENTIALS).read_text()) == {login.GRANT: GRANT}
    assert store.all()["master-account-claude-2"] == "twoatexample_com"
    assert [path for path, _, _ in bao.merges] == ["agentihooks/master-claude-login-2"]
    assert bao.merges[0][1]["epoch"] == "5"


def _environ(rendered, name="master@doctor-1"):
    return {"AGENTIHOOKS_AGENT_NAME": name, "CLAUDE_CONFIG_DIR": str(rendered), "AH_CC_TOKEN_w": "x"}


def test_two_logins_are_ranked_by_room_and_one_is_never_ranked(monkeypatch, rendered, tmp_path):
    monkeypatch.setattr(openbao_kv, "from_env", lambda environ: None)
    _stored(1, {"emailAddress": "one@example.com"})
    monkeypatch.setattr(master_logins, "left", lambda account, now: pytest.fail("one login needs no ranking"))
    assert login.prepare(_environ(rendered, "master@rig-1"), tmp_path)[0] == login.home(1)
    _stored(2, TWO)
    _room(monkeypatch, oneatexample_com=10.0, twoatexample_com=80.0)
    assert login.prepare(_environ(rendered), tmp_path)[0] == login.home(2)


def test_equal_room_prefers_the_active_slot(monkeypatch, rendered, tmp_path):
    monkeypatch.setattr(openbao_kv, "from_env", lambda environ: None)
    _stored(1, {"emailAddress": "one@example.com"})
    _stored(2, TWO)
    _room(monkeypatch, oneatexample_com=50.0, twoatexample_com=50.0)
    monkeypatch.setenv(login.SLOT_ENV, "2")
    assert login.prepare(_environ(rendered), tmp_path)[0] == login.home(2)


def test_the_operator_order_outranks_room(monkeypatch, rendered, tmp_path):
    monkeypatch.setattr(openbao_kv, "from_env", lambda environ: None)
    _stored(1, {"emailAddress": "one@example.com"})
    _stored(2, TWO)
    _room(monkeypatch, oneatexample_com=10.0, twoatexample_com=80.0)
    monkeypatch.setenv(login.ORDER_ENV, " , one@example.com,twoatexample_com")
    assert login.prepare(_environ(rendered), tmp_path)[0] == login.home(1)


def _week(monkeypatch, **used):
    monkeypatch.setattr(master_logins, "usage", lambda account, now: (0.0, used[account] + 0 * now, 0.0))


@pytest.mark.parametrize("spent", [reservation.MASTER_LEAVES, 96.0])
def test_a_login_past_the_master_limit_yields_to_the_next_in_order(monkeypatch, rendered, tmp_path, spent):
    monkeypatch.setattr(openbao_kv, "from_env", lambda environ: None)
    _stored(1, {"emailAddress": "one@example.com"})
    _stored(2, TWO)
    _room(monkeypatch, oneatexample_com=80.0, twoatexample_com=10.0)
    _week(monkeypatch, oneatexample_com=spent, twoatexample_com=25.0)
    monkeypatch.setenv(login.ORDER_ENV, "oneatexample_com,twoatexample_com")
    assert login.prepare(_environ(rendered), tmp_path)[0] == login.home(2)


def test_the_operator_order_holds_while_both_logins_are_under_the_master_limit(monkeypatch, rendered, tmp_path):
    monkeypatch.setattr(openbao_kv, "from_env", lambda environ: None)
    _stored(1, {"emailAddress": "one@example.com"})
    _stored(2, TWO)
    _room(monkeypatch, oneatexample_com=10.0, twoatexample_com=80.0)
    _week(monkeypatch, oneatexample_com=90.0, twoatexample_com=20.0)
    monkeypatch.setenv(login.ORDER_ENV, "oneatexample_com,twoatexample_com")
    assert login.prepare(_environ(rendered), tmp_path)[0] == login.home(1)


def test_the_operator_order_holds_when_every_login_is_past_the_master_limit(monkeypatch, rendered, tmp_path):
    monkeypatch.setattr(openbao_kv, "from_env", lambda environ: None)
    _stored(1, {"emailAddress": "one@example.com"})
    _stored(2, TWO)
    _room(monkeypatch, oneatexample_com=1.0, twoatexample_com=4.0)
    _week(monkeypatch, oneatexample_com=99.0, twoatexample_com=96.0)
    monkeypatch.setenv(login.ORDER_ENV, "oneatexample_com,twoatexample_com")
    assert login.prepare(_environ(rendered), tmp_path)[0] == login.home(1)


def test_a_held_first_login_passes_to_the_next_ordered_login_under_the_master_limit(monkeypatch, rendered, tmp_path):
    monkeypatch.setattr(openbao_kv, "from_env", lambda environ: None)
    _stored(1, {"emailAddress": "one@example.com"})
    _stored(2, TWO)
    _stored(3, {"emailAddress": "three@example.com"})
    _room(monkeypatch, oneatexample_com=50.0, twoatexample_com=90.0, threeatexample_com=10.0)
    _week(monkeypatch, oneatexample_com=20.0, twoatexample_com=97.0, threeatexample_com=30.0)
    monkeypatch.setenv(login.ORDER_ENV, "oneatexample_com,twoatexample_com,threeatexample_com")
    environ = _environ(rendered)
    _held(1, environ["AGENTIHOOKS_AGENT_NAME"])
    assert login.prepare(environ, tmp_path)[0] == login.home(3)


def test_no_operator_order_ranks_by_room_alone(monkeypatch, rendered, tmp_path):
    monkeypatch.setattr(openbao_kv, "from_env", lambda environ: None)
    monkeypatch.delenv(login.ORDER_ENV, raising=False)
    _stored(1, {"emailAddress": "XXXX"})
    _stored(2, TWO)
    _room(monkeypatch, xxxx=10.0, twoatexample_com=80.0)
    assert login.prepare(_environ(rendered), tmp_path)[0] == login.home(2)


def test_a_held_first_choice_stays_first_in_the_operator_order(monkeypatch, rendered, tmp_path):
    monkeypatch.setattr(openbao_kv, "from_env", lambda environ: None)
    _stored(1, {"emailAddress": "one@example.com"})
    _stored(2, TWO)
    _stored(3, {"emailAddress": "three@example.com"})
    _room(monkeypatch, oneatexample_com=10.0, twoatexample_com=20.0, threeatexample_com=90.0)
    monkeypatch.setenv(login.ORDER_ENV, "oneatexample_com,two@example.com")
    _held(1, "master@a-1")
    assert login.prepare(_environ(rendered), tmp_path)[0] == login.home(1)


def test_a_front_door_joins_multiple_holders_of_the_preferred_login(monkeypatch, rendered, tmp_path):
    monkeypatch.setattr(openbao_kv, "from_env", lambda environ: None)
    _stored(1, {"emailAddress": "one@example.com"})
    _stored(2, TWO)
    _room(monkeypatch, oneatexample_com=50.0, twoatexample_com=40.0)
    _held(1, "master@a-1", "master@b-1")
    _held(2, "master@c-1")
    assert login.prepare(_environ(rendered), tmp_path)[0] == login.home(1)
    assert set(login.holders(login.root(1))) == {
        "master@a-1",
        "master@b-1",
        _environ(rendered)["AGENTIHOOKS_AGENT_NAME"],
    }
    assert login.holders(login.root(2)) == {"master@c-1": os.getppid()}


def test_removing_the_last_slot_clears_only_that_slot(monkeypatch):
    bao = Bao(monkeypatch, {})
    _stored(1, {"emailAddress": "one@example.com"})
    _stored(login.MAX_SLOTS, TWO)
    assert login.remove(login.MAX_SLOTS, "bao") == "twoatexample_com"
    assert not login.home(login.MAX_SLOTS).exists() and login.home(1).exists()
    assert bao.merges == [(f"agentihooks/master-claude-login-{login.MAX_SLOTS}", {}, login.RECORD_KEYS)]


def test_removal_refusals_name_the_slot_and_every_holder():
    with pytest.raises(login.LoginRefused) as outside:
        login.remove(login.MAX_SLOTS + 1, None)
    assert str(outside.value) == f"master login slot {login.MAX_SLOTS + 1} is outside 1 to {login.MAX_SLOTS}"
    _stored(2, TWO)
    _held(2, "master@a-1", "master@b-1")
    with pytest.raises(login.LoginRefused) as held:
        login.remove(2, None)
    assert str(held.value) == "the master login is held by master@a-1, master@b-1, still running"


def test_removal_without_the_store_refuses_while_a_record_would_restore_it(monkeypatch, capsys):
    _stored(2, TWO)
    environ = {"OPENBAO_URL": "https://bao.example", "OPENBAO_TOKEN": "t"}
    assert claude_login.removal(2, False, environ, claude_login.Deps(console=None)) == 1
    assert (
        "the OpenBao record agentihooks/master-claude-login-2 would restore this login at the next master launch: "
        "rerun with --openbao"
    ) in capsys.readouterr().err
    assert login.home(2).exists()


def test_a_failed_record_clear_keeps_the_login(monkeypatch):
    _stored(2, TWO)

    def sealed(*_):
        raise openbao_kv.OpenBaoError(503, "sealed")

    monkeypatch.setattr(openbao_kv, "merge", sealed)
    with pytest.raises(openbao_kv.OpenBaoError):
        login.remove(2, "bao")
    assert login.home(2).exists()


def test_listing_and_removal_name_an_unknown_account_and_each_holder(monkeypatch, capsys):
    _stored(2)
    _stored(3, TWO)
    _held(3, "master@a-1", "master@b-1")
    monkeypatch.setattr(master_logins, "usage", lambda account, now: (1.0, 2.0, now))
    assert claude_login.listing() == 0
    assert capsys.readouterr().out == (
        "slot 2: unknown, free, 5h 1% used, 7d 2% used\n"
        "slot 3: twoatexample_com, master@a-1, master@b-1, 5h 1% used, 7d 2% used\n"
    )
    assert claude_login.removal(2, False, {}, claude_login.Deps(console=None)) == 0
    assert capsys.readouterr().out == "master login slot 2 (unknown) removed\n"


def test_remove_passes_the_store_choice_environment_console_and_opener(monkeypatch):
    seen = []
    monkeypatch.setattr(openbao_step, "target", lambda *args: seen.append(args) or "bao")
    monkeypatch.setattr(login, "remove", lambda number, bao: seen.append((number, bao)) or "acct")
    console, opener, environ = object(), object(), {"HOME": "/home/op"}
    assert claude_login.main(["--remove", "4", "--openbao"], environ, console=console, opener=opener) == 0
    assert seen == [(True, environ, console, opener), (4, "bao")]


def test_the_help_names_the_remove_slot():
    assert "--remove SLOT" in claude_login._parser().format_help()


def test_a_master_is_offered_every_free_login_with_its_own_room(monkeypatch):
    from hooks.context import quota_policy
    from hooks.context.quota_policy import Candidate

    _stored(1, {"emailAddress": "one@example.com"})
    _stored(2, TWO)
    _held(1, "master@rig-1")
    _held(2, "master@other-1")
    _stored(3, {"emailAddress": "three@example.com"})
    used = {"oneatexample_com": (40.0, 98.0, 5.0), "threeatexample_com": (1.0, 2.0, 6.0)}
    monkeypatch.setattr(master_logins, "usage", lambda account, now: (*used[account][:2], used[account][2] + 0 * now))
    assert quota_policy._login_accounts({"AGENTIHOOKS_AGENT_NAME": "master@rig-1"}) == [
        Candidate("oneatexample_com", 40.0, 98.0, 0, 5.0, 1, login=True),
        Candidate("threeatexample_com", 1.0, 2.0, 0, 6.0, 1, login=True),
    ]


def _window(used, resets_at):
    return SimpleNamespace(used=used, resets_at=resets_at)


def _probe(account, five, week):
    return SimpleNamespace(account=account, five_hour=_window(*five), seven_day=_window(*week))


def _limits(five, week):
    return {
        "five_hour": {"used_percentage": five[0], "resets_at": five[1]},
        "seven_day": {"used_percentage": week[0], "resets_at": week[1]},
    }


def _readings(monkeypatch, cache, snapshot):
    from hooks.context import quota_usage
    from scripts import claude_quota_balancer

    monkeypatch.setattr(claude_quota_balancer, "cached_observations", lambda: cache)
    monkeypatch.setattr(quota_usage, "latest_snapshot", lambda account: snapshot if account == "two" else None)


def test_room_reads_the_newest_snapshot_and_drops_a_window_that_reset(monkeypatch):
    cache = [(100.0, _probe("two", (10.5, 900), (20.5, 900))), (500.0, _probe("other", (90, 900), (90, 900)))]
    _readings(monkeypatch, cache, (200.0, _limits((30.5, 900), (40.5, 300))))
    assert master_logins.usage("two", 300.0) == (30.5, 0.0, 200.0)
    assert master_logins.left("two", 300.0) == 69.5


def test_room_reads_the_cache_when_it_is_newer(monkeypatch):
    _readings(
        monkeypatch, [(300.0, _probe("two", (10.5, 900), (20.5, 299.5)))], (200.0, _limits((30.5, 900), (5, 900)))
    )
    assert master_logins.usage("two", 299.0) == (10.5, 20.5, 300.0)
    assert master_logins.left("two", 299.0) == 79.5


def test_room_reads_a_snapshot_week_and_drops_its_five_hours_that_reset(monkeypatch):
    _readings(
        monkeypatch, [(100.0, _probe("two", (10.5, 900), (20.5, 900)))], (200.0, _limits((30.5, 250), (40.5, 900)))
    )
    assert master_logins.usage("two", 300.0) == (0.0, 40.5, 200.0)


def test_a_cache_reading_and_snapshot_seen_at_once_compare_only_their_time(monkeypatch):
    _readings(
        monkeypatch, [(200.0, _probe("two", (None, 900), (20.5, 900)))], (200.0, _limits((30.5, 900), (40.5, 900)))
    )
    assert master_logins.usage("two", 300.0) == (0.0, 20.5, 200.0)


def test_a_login_never_seen_or_without_numbers_reads_as_unused(monkeypatch):
    _readings(monkeypatch, [(300.0, _probe("two", (None, None), (None, 900)))], None)
    assert master_logins.usage("two", 1.0) == (0.0, 0.0, 300.0)
    assert master_logins.usage("absent", 1.0) == (0.0, 0.0, 0.0)
    assert master_logins.left("absent", 1.0) == 100.0


def test_a_used_number_counts_until_its_reset_passes():
    assert master_logins._used(12, 50, 49.5) == 12.0
    assert master_logins._used(12.5, 50.5, 50.5) == 0.0
    assert master_logins._used(12.5, None, 50.0) == 12.5
    assert master_logins._used("12", 10, 50.0) == 0.0
