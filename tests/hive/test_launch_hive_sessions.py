import json

from scripts.hive import launch
from scripts.swarm.capacity import Account


def _rows():
    return [
        Account("claude", "full_on_hives", "room", 0, 80, 80, 6),
        Account("claude", "room_left", "room", 0, 80, 80, 6),
        Account("codex", "other", "room", 0, 80, 80, 6),
    ]


def test_an_account_the_hives_already_fill_has_no_open_seat():
    hived = {"full-on-hives": 6, "room-left": 5}

    assert [row.name for row in launch._open_claude_rows(_rows(), hived)] == ["room_left"]


def test_hive_sessions_count_each_remote_hive_once_and_skip_this_machine(monkeypatch):
    import fakeredis

    from scripts.hive import registry

    redis = fakeredis.FakeRedis(decode_responses=True)
    hives = [{"id": "local", "heartbeat_at": 10}, {"id": "remote", "heartbeat_at": 10}]
    sessions = {
        "local": {"claude:room_left": 4},
        "remote": {"claude:full_on_hives": 6, "codex:other": 2, "interactive:claude:room_left": 1, "_executions": {}},
    }
    for hive, counts in sessions.items():
        redis.set(f"{registry.key(hive)}:sessions", json.dumps(counts))
    monkeypatch.setattr("scripts.routing.aggregate.enabled", lambda: False)
    monkeypatch.setattr(registry, "hives", lambda r: hives)
    monkeypatch.setattr(registry, "live", lambda hive, now: True)
    monkeypatch.setattr("scripts.swarm.commands.hive_id", lambda: "local")

    assert launch._hive_sessions(redis, 0) == {"full-on-hives": 6}


def test_an_aggregated_router_counts_hive_sessions_once(monkeypatch):
    monkeypatch.setattr("scripts.routing.aggregate.enabled", lambda: True)
    monkeypatch.setattr("scripts.hive.registry.hives", lambda redis: (_ for _ in ()).throw(AssertionError))

    assert launch._hive_sessions(None, 0) == {}
