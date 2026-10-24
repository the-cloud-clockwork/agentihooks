import json

import pytest

from scripts.hive import launch, placement
from scripts.swarm.capacity import Account

pytestmark = pytest.mark.fakeredis


def _rows():
    return [
        Account("claude", "full_on_hives", "room", 0, 80, 80, 6),
        Account("claude", "room_left", "room", 0, 80, 80, 6),
        Account("codex", "other", "room", 0, 80, 80, 6),
        Account("claude", "last_seat", "room", 5, 80, 80, 6),
    ]


def test_an_account_the_hives_already_fill_has_no_open_seat():
    hived = {"full-on-hives": 6, "room-left": 5}

    assert [row.name for row in launch._open_claude_rows(_rows(), hived)] == ["room_left", "last_seat"]


def test_an_outright_account_beats_an_earlier_fallback_and_the_first_fallback_is_kept():
    rows = _rows()
    picks = {
        "full_on_hives": placement.Choice("anton", "no live UI hive takes master"),
        "room_left": placement.Choice("ws"),
        "other": placement.Choice("pc", "second fallback"),
        "last_seat": placement.Wait("no slot"),
    }

    assert launch._first_choice(rows, picks.__getitem__) == (rows[1], picks["room_left"])
    del picks["room_left"]
    fallbacks = [rows[0], rows[2], rows[3]]
    assert launch._first_choice(fallbacks, picks.__getitem__) == (rows[0], picks["full_on_hives"])
    assert launch._first_choice([rows[3]], picks.__getitem__) == picks["last_seat"]
    busier = {"last_seat": placement.Choice("anton", "busier fallback"), "other": picks["other"]}
    assert launch._first_choice([rows[3], rows[2]], busier.__getitem__) == (rows[2], picks["other"])
    assert launch._first_choice([], picks.__getitem__) == placement.Wait("no advertised Claude account has room")


def test_hive_sessions_count_each_remote_hive_once_and_skip_this_machine(monkeypatch):
    import fakeredis

    from scripts.hive import registry

    redis = fakeredis.FakeRedis(decode_responses=True)
    hives = [{"id": name, "heartbeat_at": 10} for name in ("local", "remote", "second", "stale", "empty")]
    sessions = {
        "local": {"claude:room_left": 4},
        "remote": {"claude:full_on_hives": 6, "codex:other": 2, "interactive:claude:room_left": 1, "_executions": {}},
        "second": {"claude:full_on_hives": 2, "claude:room_left": 1, "claude:account:label": 3, "claude:": 99},
        "stale": {"claude:room_left": 99},
    }
    for hive, counts in sessions.items():
        redis.set(f"{registry.key(hive)}:sessions", json.dumps(counts))
    monkeypatch.setattr("scripts.routing.aggregate.enabled", lambda: False)
    monkeypatch.setattr(registry, "hives", lambda r: hives)
    monkeypatch.setattr(registry, "live", lambda hive, now: hive["id"] != "stale")
    monkeypatch.setattr("scripts.swarm.commands.hive_id", lambda: "local")

    assert launch._hive_sessions(redis, 0) == {"full-on-hives": 8, "room-left": 1, "account:label": 3}


def test_an_aggregated_router_counts_hive_sessions_once(monkeypatch):
    monkeypatch.setattr("scripts.routing.aggregate.enabled", lambda: True)
    monkeypatch.setattr("scripts.hive.registry.hives", lambda redis: (_ for _ in ()).throw(AssertionError))

    assert launch._hive_sessions(None, 0) == {}


def test_hive_sessions_count_launch_reservations_no_hive_reports_yet(monkeypatch):
    import fakeredis

    from scripts.hive import registry
    from scripts.swarm.keyspace import ROOT

    redis = fakeredis.FakeRedis(decode_responses=True)
    hives = [{"id": "local", "heartbeat_at": 10}, {"id": "remote", "heartbeat_at": 10}]
    redis.set(
        f"{registry.key('local')}:sessions",
        json.dumps({"claude:room_left": 1, "_executions": {"claude:room_left": ["exe-local"]}}),
    )
    redis.set(
        f"{registry.key('remote')}:sessions",
        json.dumps({"claude:room_left": 2, "_executions": {"claude:room_left": ["exe-remote", "exe-gone"]}}),
    )
    rows = {
        "exe-local": {"slot": "claude:room_left", "state": "live"},
        "exe-remote": {"slot": "claude:room_left", "state": "live"},
        "exe-pending": {"slot": "claude:room_left", "state": "reserved"},
        "exe-second": {"slot": "claude:full_on_hives", "state": "reserved"},
        "exe-codex": {"slot": "codex:other", "state": "reserved"},
        "exe-gone": {"slot": "claude:room_left", "state": "exited"},
        "exe-ended": {"slot": "claude:full_on_hives", "state": "exited"},
    }
    for execution, row in rows.items():
        redis.set(f"{ROOT}:launch-slot:{execution}", json.dumps(row))
        redis.sadd(f"{ROOT}:launch-slots", execution)
    monkeypatch.setattr("scripts.routing.aggregate.enabled", lambda: False)
    monkeypatch.setattr(registry, "hives", lambda r: hives)
    monkeypatch.setattr(registry, "live", lambda hive, now: True)
    monkeypatch.setattr("scripts.swarm.commands.hive_id", lambda: "local")

    assert launch._hive_sessions(redis, 0) == {"room-left": 2, "full-on-hives": 1}
