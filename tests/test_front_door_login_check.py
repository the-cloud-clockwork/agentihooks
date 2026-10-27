import json
import os
from types import SimpleNamespace

import pytest

from scripts import master_claude_login as login
from scripts import master_logins, terminate_agent
from scripts.feature_checks import front_door_login as check


@pytest.fixture(autouse=True)
def ah(tmp_path, monkeypatch):
    monkeypatch.setattr(login.hooks_config, "AGENTIHOOKS_HOME", str(tmp_path / "ah"))
    monkeypatch.delenv(login.SLOT_ENV, raising=False)
    monkeypatch.delenv(login.ORDER_ENV, raising=False)
    monkeypatch.setattr(
        terminate_agent,
        "sessions",
        lambda: [SimpleNamespace(name=name) for name in ("master@rig-1", "master@doctor-1", "master@token")],
    )
    return tmp_path / "ah"


def _stored(number, email, *holders):
    home = login.home(number)
    home.mkdir(parents=True)
    (home / login.CREDENTIALS).write_text(json.dumps({login.GRANT: {"refreshToken": email}}))
    (home / login.CONFIG).write_text(json.dumps({login.ACCOUNT: {"emailAddress": email}}))
    (login.root(number) / login.OWNER).write_text(json.dumps({"holders": dict.fromkeys(holders, os.getpid())}))


def _room(monkeypatch, **left):
    monkeypatch.setattr(master_logins, "left", lambda account, now: left[account] + 0 * now)


def _agent(name, seat="", lane="master", state="working", account=""):
    return SimpleNamespace(name=name, seat=seat, lane=lane, state=state, harness="claude", account=account)


class Store:
    def __init__(self, swarms, state="running"):
        self.swarms = swarms
        self.state = state

    def slugs(self):
        return sorted(self.swarms)

    def config(self, slug):
        return SimpleNamespace(state=self.state)

    def agents(self, slug):
        return self.swarms[slug]


def _store():
    return Store(
        {
            "rig": [_agent("master@rig-1", account="tokenatexample_com"), _agent("eng@rig-2", lane="eng")],
            "doctor": [_agent("master@doctor-1"), _agent("master@doctor-2", seat="master-2@doctor")],
        }
    )


def test_a_front_door_on_a_token_while_the_stored_login_has_room_fails(monkeypatch, capsys):
    _stored(1, "one@example.com", "master@doctor-1")
    _room(monkeypatch, oneatexample_com=60.0)
    assert check.main(_store()) == 1
    shown = capsys.readouterr().out
    assert "rig front door master@rig-1 runs on a quota token while the stored login oneatexample_com has room" in shown
    assert "refreshToken" not in shown and "one@example.com" not in shown


def test_front_doors_sharing_the_preferred_login_pass(monkeypatch, capsys):
    _stored(1, "one@example.com", "master@rig-1", "master@doctor-1")
    _stored(2, "two@example.com")
    _room(monkeypatch, oneatexample_com=60.0, twoatexample_com=90.0)
    monkeypatch.setenv(login.ORDER_ENV, "one@example.com,two@example.com")
    assert check.main(_store()) == 0
    shown = capsys.readouterr().out
    assert "rig front door master@rig-1 runs on the claude ai login oneatexample_com" in shown
    assert "master@doctor-2" not in shown


def test_a_token_front_door_passes_when_every_stored_login_is_spent(monkeypatch):
    _stored(1, "one@example.com", "master@doctor-1")
    _room(monkeypatch, oneatexample_com=2.0)
    assert check.violations(check.front_doors(_store()), check.ordered(1.0)) == []


def test_front_doors_on_different_logins_fail_while_the_preferred_has_room(monkeypatch):
    _stored(1, "one@example.com", "master@doctor-1")
    _stored(2, "two@example.com", "master@rig-1")
    _room(monkeypatch, oneatexample_com=60.0, twoatexample_com=90.0)
    monkeypatch.setenv(login.ORDER_ENV, "one@example.com,two@example.com")
    assert check.violations(check.front_doors(_store()), check.ordered(1.0)) == [
        "front doors sit on different logins, oneatexample_com and twoatexample_com, "
        "while the preferred login oneatexample_com has room"
    ]


def test_front_doors_may_spread_once_the_preferred_login_is_spent(monkeypatch):
    _stored(1, "one@example.com", "master@doctor-1")
    _stored(2, "two@example.com", "master@rig-1")
    _room(monkeypatch, oneatexample_com=3.0, twoatexample_com=90.0)
    monkeypatch.setenv(login.ORDER_ENV, "one@example.com,two@example.com")
    assert check.violations(check.front_doors(_store()), check.ordered(1.0)) == []


def test_live_routes_keep_the_swarm_front_door_and_account(monkeypatch):
    _stored(1, "one@example.com", "master@rig-1")
    current = _store()
    current.swarms["rig"][0].seat = "master@rig"
    _stored(2, "two@example.com", "master@doctor-1")
    assert check.front_doors(current) == [
        check.FrontDoor("doctor", "master@doctor-1", "twoatexample_com", ""),
        check.FrontDoor("rig", "master@rig-1", "oneatexample_com", "tokenatexample_com"),
    ]
    assert check.front_doors(Store({"rig": [_agent("master@token", account="route")]})) == [
        check.FrontDoor("rig", "master@token", "", "route")
    ]

@pytest.mark.parametrize("state", ["stopped", "paused"])
def test_front_doors_excludes_swarms_that_are_not_running(state, monkeypatch):
    _stored(1, "one@example.com")
    _room(monkeypatch, oneatexample_com=60)
    current = Store({"rig": [_agent("master@rig-1", account="token")]}, state=state)
    assert check.front_doors(current) == []
    assert check.main(current) == 0


def test_front_doors_excludes_a_working_master_whose_session_is_gone(monkeypatch):
    _stored(1, "one@example.com")
    _room(monkeypatch, oneatexample_com=60)
    monkeypatch.setattr(terminate_agent, "sessions", lambda: [])
    current = Store({"rig": [_agent("master@rig-1", account="token")]})
    assert check.front_doors(current) == []
    assert check.main(current) == 0


def test_login_order_uses_preference_then_room_then_slot(monkeypatch):
    monkeypatch.setattr(login, "logins", lambda: {1: "z", 2: "a", 3: "first", 4: "more"})
    monkeypatch.setattr(login, "_preferred", lambda: {"first": 0})
    _room(monkeypatch, z=30, a=30, first=10, more=60)
    assert check.ordered(1) == [("first", 10), ("more", 60), ("z", 30), ("a", 30)]


def test_default_main_reads_live_store_and_reports_all_routes(monkeypatch, capsys):
    from scripts.swarm import store

    connection = object()
    current = _store()
    seen = []
    monkeypatch.setattr(store, "redis_client", lambda: connection)

    def connected(value):
        seen.append(value)
        return current

    monkeypatch.setattr(store, "RedisStore", connected)
    _stored(1, "one@example.com", "master@doctor-1")
    _stored(2, "two@example.com")
    _room(monkeypatch, oneatexample_com=60, twoatexample_com=20)
    assert check.main() == 1
    assert seen == [connection]
    assert capsys.readouterr().out.splitlines() == [
        "stored logins in order: oneatexample_com 60% left, twoatexample_com 20% left",
        "doctor front door master@doctor-1 runs on the claude ai login oneatexample_com",
        "rig front door master@rig-1 runs on a quota token tokenatexample_com",
        "FAIL: rig front door master@rig-1 runs on a quota token while the stored login oneatexample_com has room",
    ]


def test_no_stored_logins_reports_unknown_token_route(capsys):
    assert check.main(Store({"rig": [_agent("master@unknown")]})) == 0
    assert capsys.readouterr().out.splitlines() == [
        "stored logins in order: none",
        "rig front door master@unknown runs on a quota token unknown",
    ]
