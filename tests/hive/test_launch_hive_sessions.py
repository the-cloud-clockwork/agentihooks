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


def test_hive_sessions_are_read_from_every_live_hive(monkeypatch):
    monkeypatch.setattr("scripts.routing.aggregate.enabled", lambda: False)
    monkeypatch.setattr(
        "scripts.routing.aggregate.read",
        lambda redis, now: {
            "claude:full_on_hives": {"harness": "claude", "account": "full_on_hives", "sessions": 6},
            "codex:other": {"harness": "codex", "account": "other", "sessions": 2},
        },
    )

    assert launch._hive_sessions(None, 0) == {"full-on-hives": 6}


def test_an_aggregated_router_counts_hive_sessions_once(monkeypatch):
    monkeypatch.setattr("scripts.routing.aggregate.enabled", lambda: True)
    monkeypatch.setattr("scripts.routing.aggregate.read", lambda redis, now: (_ for _ in ()).throw(AssertionError))

    assert launch._hive_sessions(None, 0) == {}
