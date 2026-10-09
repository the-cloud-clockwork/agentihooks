import json

import pytest

from scripts.swarm import cli, host_budget, tick
from scripts.swarm.store import MASTER, RedisStore, SwarmConfig
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = pytest.mark.xdist_group("fakeredis")

CAPS = {"eng": 2, "ci": 1, "plan": 0}
MEMORY = "2100 MB available memory fits 3 at 700 MB each"
UNKNOWN = "host unknown: the process files cannot be read, so spawns pass"


@pytest.fixture
def store():
    import fakeredis

    found = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    found.create(SwarmConfig("sw", "/repo", max_eng=2, max_ci=1))
    return found


def _decide(store, room, reason=MEMORY, limit="memory", slug="sw"):
    decision = {
        "configured": CAPS,
        "effective": CAPS,
        "reason": "accounts have quota",
        "accounts": [],
        "at": 1_000,
        "host": {"room": room, "reason": reason, "limit": limit},
    }
    store.redis.set(store.key(slug, "quota-capacity"), json.dumps(decision))


def _ledger():
    return FakeLedger([{"id": "t1", "lane": "eng"}, {"id": "t2", "lane": "eng"}, {"id": "t3", "lane": "ci"}])


def _workers(runtime):
    return [task for _, _, task in runtime.spawned]


def _room(store, slug="sw"):
    return json.loads(store.redis.get(store.key(slug, "quota-capacity")))["host"]["room"]


def _held(room, spawned, reason=MEMORY, limit="memory", who="spawns"):
    return f"holding {who}: host {limit} room {room}, {spawned} spawned in the last 2 minutes: {reason}"


def test_manual_caps_above_the_host_room_spawn_up_to_the_room_and_the_rest_once_room_returns(store):
    ledger, runtime = _ledger(), FakeRuntime()
    _decide(store, 2)
    actions = tick.tick("sw", store, ledger, runtime, now_ms=1_000)
    assert [name for name, _ in runtime.masters] == ["master@a1b2c3-0001"]
    assert _workers(runtime) == ["t1"]
    assert _held(2, 2) in actions
    assert tick.spawn_holds(store, "sw") == [_held(2, 2)]
    assert _room(store) == 2
    _decide(store, 2)
    actions = tick.tick("sw", store, ledger, runtime, now_ms=61_000)
    assert _workers(runtime) == ["t1"]
    assert _held(2, 2) in actions
    _decide(store, 2)
    actions = tick.tick("sw", store, ledger, runtime, now_ms=121_000)
    assert _workers(runtime) == ["t1", "t3", "t2"]
    assert not any(action.startswith("holding spawns") for action in actions)
    assert tick.spawn_holds(store, "sw") == []


def test_spawns_in_one_swarm_use_the_host_room_of_every_swarm(store):
    _decide(store, 2)
    tick.tick("sw", store, _ledger(), FakeRuntime(), now_ms=1_000)
    store.create(SwarmConfig("doc", "/repo", max_eng=2, max_ci=1))
    _decide(store, 2, slug="doc")
    ledger = _ledger()
    ledger.notify = lambda slug, text: None
    runtime = FakeRuntime()
    actions = tick.tick("doc", store, ledger, runtime, now_ms=30_000)
    assert _held(2, 2, who="the master spawn") in actions
    assert runtime.masters == [] and runtime.spawned == []


def test_an_unknown_host_lets_every_spawn_pass(store):
    ledger, runtime = _ledger(), FakeRuntime()
    _decide(store, None, UNKNOWN, "unknown")
    actions = tick.tick("sw", store, ledger, runtime, now_ms=1_000)
    assert _workers(runtime) == ["t1", "t3", "t2"]
    assert not any(action.startswith("holding") for action in actions)
    assert _room(store) is None


def test_a_decision_without_a_host_reading_never_holds(store):
    ledger, runtime = _ledger(), FakeRuntime()
    store.redis.set(store.key("sw", "quota-capacity"), json.dumps({"effective": CAPS}))
    tick.tick("sw", store, ledger, runtime, now_ms=1_000)
    assert _workers(runtime) == ["t1", "t3", "t2"]


def test_the_master_seat_waits_for_host_room(store):
    runtime = FakeRuntime()
    reason = "one minute load 1.60 per CPU is above the high watermark 1.50, no room"
    _decide(store, 0, reason, "load")
    actions = tick.tick("sw", store, FakeLedger([]), runtime, now_ms=1_000)
    held = _held(0, 0, reason, "load", "the master spawn")
    assert held in actions
    assert tick.spawn_holds(store, "sw") == [held]
    assert runtime.masters == []
    assert [a for a in store.agents("sw") if a.lane == MASTER] == []
    _decide(store, 1)
    actions = tick.tick("sw", store, FakeLedger([]), runtime, now_ms=61_000)
    assert "spawned master master@a1b2c3-0001" in actions
    assert store.redis.zrange(tick.HOST_SPENDS, 0, -1, withscores=True) == [("master@a1b2c3-0001", 61_000.0)]


def test_a_spawn_leaves_the_window_once_it_settles(store):
    tick._spend_host(store, "old", 1_000)
    tick._spend_host(store, "new", 1_000 + tick.HOST_SETTLE_MS)
    assert store.redis.zrange(tick.HOST_SPENDS, 0, -1) == ["new"]
    assert tick.HOST_SETTLE_MS == 120_000


def test_the_quota_seat_check_runs_before_the_host_gate(store):
    _decide(store, 0)
    actions = tick.tick("sw", store, _ledger(), FakeRuntime(full=True), now_ms=1_000)
    assert "no session slot for the master, waiting" in actions
    assert not any(action.startswith("holding") for action in actions)


def test_a_paused_tick_clears_an_old_hold(store):
    _decide(store, 2)
    tick.tick("sw", store, _ledger(), FakeRuntime(), now_ms=1_000)
    store.update("sw", state="paused")
    tick.tick("sw", store, _ledger(), FakeRuntime(), now_ms=61_000)
    assert tick.spawn_holds(store, "sw") == []


def test_swarm_status_names_the_host_room_and_the_held_spawn(store, monkeypatch, capsys):
    ledger = _ledger()
    _decide(store, 2)
    tick.tick("sw", store, ledger, FakeRuntime(), now_ms=1_000)
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setattr(cli, "LedgerClient", lambda: ledger)
    assert cli.main(["sw", "status"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert f"host room 2: {MEMORY}" in lines
    assert _held(2, 2) in lines


def test_swarm_status_names_an_unknown_host(store, monkeypatch, capsys):
    _decide(store, None, UNKNOWN, "unknown")
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setattr(cli, "LedgerClient", lambda: _ledger())
    assert cli.main(["sw", "status"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert f"host room unknown: {UNKNOWN}" in lines
    assert not any(line.startswith("holding spawns") for line in lines)


def test_the_unknown_reason_is_the_one_the_budget_writes():
    assert host_budget.spawn_room(SwarmConfig("sw", "/repo", 1, 0), None, 4).reason == UNKNOWN
