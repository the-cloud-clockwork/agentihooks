import json
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts.swarm_v2.herdr import visibility
from scripts.swarm_v2.herdr.visibility import CACHED, FRESH_MS, KEY, LIVE, UNVERIFIED, View, judged

NOW = 1_790_000_100_000
AGENT = "engineer@a1b2c3-0001"


def clock(now_ms):
    return SimpleNamespace(time_ns=lambda: now_ms * 1_000_000)


def live(checked_at=NOW, **changes):
    return View(AGENT, "sv2-exe-a", "w1:p1", LIVE, checked_at, checked_at, **changes)


@pytest.fixture
def store():
    import fakeredis

    from scripts.swarm.store import RedisStore, SwarmConfig

    result = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    result.create(SwarmConfig("sw", ".", 0, 0, state="paused"))
    return result


def test_constants():
    assert (KEY, FRESH_MS, LIVE, CACHED, UNVERIFIED) == ("terminals", 30_000, "live", "cached", "unverified")
    assert View(AGENT, "m", "w1:p1", LIVE) == View(AGENT, "m", "w1:p1", LIVE, 0, 0, "", 30_000)


def test_a_live_view_stays_live_only_inside_its_window_either_side_of_the_reader_clock():
    view = live()
    assert judged(view, NOW + FRESH_MS) == view
    assert judged(view, NOW - FRESH_MS) == view
    assert judged(view, NOW + FRESH_MS + 1) == View(AGENT, "sv2-exe-a", "w1:p1", CACHED, NOW, NOW, "stale")
    assert judged(view, NOW - FRESH_MS - 1).state == CACHED
    assert judged(live(fresh_ms=10), NOW + 11).state == CACHED


def test_a_live_state_whose_latest_check_failed_is_cached():
    view = View(AGENT, "m", "w1:p1", LIVE, NOW, NOW - 1)
    assert judged(view, NOW) == View(AGENT, "m", "w1:p1", CACHED, NOW, NOW - 1, "stale")


def test_a_failed_view_keeps_its_reason_and_reads_cached_or_unverified():
    seen = View(AGENT, "m", "w1:p1", CACHED, NOW, NOW - 5, "unreachable")
    assert judged(seen, NOW) == seen
    never = View(AGENT, "m", "w1:p1", CACHED, NOW, 0, "unreachable")
    assert judged(never, NOW) == View(AGENT, "m", "w1:p1", UNVERIFIED, NOW, 0, "unreachable")
    fresh = View(AGENT, "m", "w1:p1", UNVERIFIED)
    assert judged(fresh, NOW) == View(AGENT, "m", "w1:p1", UNVERIFIED, 0, 0, "never_live")


def test_publish_replaces_every_row_without_screen_text(store):
    other = View("engineer@a1b2c3-0002", "sv2-exe-b", "w1:p1", CACHED, NOW, NOW - 9, "unreachable")
    visibility.publish(store, "sw", [live(), other])
    raw = store.redis.hgetall(store.key("sw", "terminals"))
    assert {agent: json.loads(row) for agent, row in raw.items()} == {
        AGENT: asdict(live()),
        other.agent: asdict(other),
    }
    visibility.publish(store, "sw", [other])
    assert set(store.redis.hgetall(store.key("sw", "terminals"))) == {other.agent}
    visibility.publish(store, "sw", [])
    assert store.redis.exists(store.key("sw", "terminals")) == 0


def test_read_judges_every_row_at_the_reader_clock_in_agent_order(store):
    visibility.publish(store, "sw", [live(), View("b", "m", "w1:p1", LIVE, NOW, NOW)])
    rows = visibility.read(store, "sw", NOW + FRESH_MS + 1)
    assert list(rows) == ["b", AGENT]
    assert rows[AGENT] == asdict(judged(live(), NOW + FRESH_MS + 1))
    assert rows["b"]["state"] == CACHED
    assert visibility.read(store, "sw", NOW)[AGENT] == asdict(live())
    assert visibility.read(store, "other", NOW) == {}


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[1, 2]",
        json.dumps({**asdict(live()), "screen": "secret"}),
        json.dumps({**asdict(live()), "checked_at": "soon", "live_at": "soon"}),
    ],
)
def test_an_unreadable_row_reads_unverified(store, raw):
    store.redis.hset(store.key("sw", "terminals"), AGENT, raw)
    assert visibility.read(store, "sw", NOW) == {AGENT: {"agent": AGENT, "state": UNVERIFIED, "reason": "unreadable"}}


def test_the_swarm_view_carries_terminals_judged_now(store):
    from scripts.swarm import commands

    visibility.publish(store, "sw", [live()])
    with patch.object(commands, "time", clock(NOW)):
        assert commands.view(store, "sw")["terminals"] == {AGENT: asdict(live())}
    with patch.object(commands, "time", clock(NOW + FRESH_MS + 1)):
        assert commands.view(store, "sw")["terminals"][AGENT]["state"] == CACHED


def test_the_swarm_view_reads_whole_milliseconds(store):
    from scripts.swarm import commands

    visibility.publish(store, "sw", [live()])
    edge = SimpleNamespace(time_ns=lambda: (NOW + FRESH_MS) * 1_000_000 + 999_999)
    with patch.object(commands, "time", edge):
        assert commands.view(store, "sw")["terminals"][AGENT]["state"] == LIVE


def test_the_ledger_swarm_status_shows_a_published_live_view_as_cached_once_it_ages(store):
    from scripts.swarm import commands
    from scripts.swarm_ledger import ledger_server

    visibility.publish(store, "sw", [live()])
    with (
        patch.object(ledger_server, "swarm_store", return_value=store),
        patch.object(commands, "time", clock(NOW + FRESH_MS + 1)),
    ):
        status = ledger_server.swarm_status("sw")
    assert status["terminals"][AGENT]["state"] == CACHED
    assert status["terminals"][AGENT]["live_at"] == NOW
