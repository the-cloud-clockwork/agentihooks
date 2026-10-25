import json
import os
from types import SimpleNamespace

import pytest
from scripts.feature_checks import front_door_login as check

from scripts import master_claude_login as login
from scripts import master_logins


@pytest.fixture(autouse=True)
def ah(tmp_path, monkeypatch):
    monkeypatch.setattr(login.hooks_config, "AGENTIHOOKS_HOME", str(tmp_path / "ah"))
    monkeypatch.delenv(login.SLOT_ENV, raising=False)
    monkeypatch.delenv(login.ORDER_ENV, raising=False)
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
    def __init__(self, swarms):
        self.swarms = swarms

    def slugs(self):
        return sorted(self.swarms)

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
