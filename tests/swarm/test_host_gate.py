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


def _decide(store, room, reason=MEMORY, limit="memory", slug="sw", granted_at=1_000):
    decision = {
        "configured": CAPS,
        "effective": CAPS,
        "reason": "accounts have quota",
        "accounts": [],
        "at": 1_000,
        "host": {"room": room, "reason": reason, "limit": limit, "granted_at": granted_at},
    }
    store.redis.set(store.key(slug, "quota-capacity"), json.dumps(decision))


def _ledger():
    return FakeLedger([{"id": "t1", "lane": "eng"}, {"id": "t2", "lane": "eng"}, {"id": "t3", "lane": "ci"}])


def _workers(runtime):
    return [task for _, _, task in runtime.spawned]


def _room(store, slug="sw"):
    return json.loads(store.redis.get(store.key(slug, "quota-capacity")))["host"]["room"]


def _held(room, spawned, reason=MEMORY, limit="memory", who="spawns"):
    return f"holding {who}: host {limit} room {room}, {spawned} spawned since it was granted: {reason}"


def test_manual_caps_above_the_host_room_spawn_up_to_the_room_and_the_rest_once_room_returns(store):
    ledger, runtime = _ledger(), FakeRuntime()
    _decide(store, 2)
    actions = tick.tick("sw", store, ledger, runtime, now_ms=1_000)
    assert [name for name, _ in runtime.masters] == ["master@a1b2c3-0001"]
    assert _workers(runtime) == ["t1"]
    assert _held(2, 2) in actions
    assert tick.spawn_holds(store, "sw") == [_held(2, 2)]
    assert _room(store) == 2
    actions = tick.tick("sw", store, ledger, runtime, now_ms=181_000)
    assert _workers(runtime) == ["t1"]
    assert _held(2, 2) in actions
    _decide(store, 2, granted_at=241_000)
    actions = tick.tick("sw", store, ledger, runtime, now_ms=241_000)
    assert _workers(runtime) == ["t1", "t3", "t2"]
    assert not any(action.startswith("holding spawns") for action in actions)
    assert tick.spawn_holds(store, "sw") == []


def test_another_swarm_counts_spawns_from_the_startup_lag_before_its_grant(store):
    _decide(store, 2)
    tick.tick("sw", store, _ledger(), FakeRuntime(), now_ms=1_000)
    store.create(SwarmConfig("doc", "/repo", max_eng=2, max_ci=1))
    ledger = FakeLedger([])
    ledger.notify = lambda slug, text: None
    runtime = FakeRuntime()
    _decide(store, 2, slug="doc", granted_at=1_000 + tick.HOST_START_LAG_MS)
    actions = tick.tick("doc", store, ledger, runtime, now_ms=31_000)
    assert _held(2, 2, who="the master spawn") in actions
    assert runtime.masters == [] and runtime.spawned == []
    _decide(store, 2, slug="doc", granted_at=1_001 + tick.HOST_START_LAG_MS)
    actions = tick.tick("doc", store, ledger, runtime, now_ms=31_001)
    ((name, _),) = runtime.masters
    assert f"spawned master {name}" in actions
    assert tick.HOST_START_LAG_MS == 30_000


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
    _decide(store, 1, granted_at=61_000)
    actions = tick.tick("sw", store, FakeLedger([]), runtime, now_ms=61_000)
    assert "spawned master master@a1b2c3-0001" in actions
    assert store.redis.zrange(tick.HOST_SPENDS, 0, -1, withscores=True) == [("master@a1b2c3-0001", 61_000.0)]


def test_only_the_newest_thousand_spawns_are_kept(store):
    for n in range(tick.HOST_SPENDS_KEPT + 1):
        tick._spend_host(store, f"agent-{n}", 1_000 + n)
    kept = store.redis.zrange(tick.HOST_SPENDS, 0, -1)
    assert len(kept) == tick.HOST_SPENDS_KEPT == 1000
    assert (kept[0], kept[-1]) == ("agent-1", "agent-1000")


def test_a_spawn_at_the_tick_time_counts_and_a_later_one_does_not(store):
    _decide(store, 1, granted_at=50_000)
    tick._spend_host(store, "later", 60_001)
    assert tick._host_full("sw", store, 60_000) == ""
    tick._spend_host(store, "now", 60_000)
    assert tick._host_full("sw", store, 60_000) == f"host memory room 1, 1 spawned since it was granted: {MEMORY}"


def test_host_spent_counts_from_the_start_lag_before_since_through_now(store):
    for name, at in (("early", 29_999), ("lagged", 30_000), ("now", 60_000), ("later", 60_001)):
        tick._spend_host(store, name, at)
    assert tick.host_spent(store, 60_000, 60_000) == 2
    assert tick.host_spent(store, 60_001, 60_001) == 2


def test_the_spawn_gate_closes_exactly_when_autoscale_has_no_room_left(store):
    from scripts.swarm import capacity

    _decide(store, 2, granted_at=50_000)
    host = capacity.read(store, "sw")["host"]
    counter = capacity.spawn_counter(store, 60_000)
    seen = []
    for name in ("first", "second"):
        seen.append((capacity.unspent(host, counter), tick._host_full("sw", store, 60_000)))
        tick._spend_host(store, name, 60_000)
    seen.append((capacity.unspent(host, counter), tick._host_full("sw", store, 60_000)))
    assert seen == [(2, ""), (1, ""), (0, f"host memory room 2, 2 spawned since it was granted: {MEMORY}")]


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
