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


def _decide(store, room, reason=MEMORY, limit="memory"):
    decision = {
        "configured": CAPS,
        "effective": CAPS,
        "reason": "accounts have quota",
        "accounts": [],
        "at": 1_000,
        "host": {"room": room, "reason": reason, "limit": limit},
    }
    store.redis.set(store.key("sw", "quota-capacity"), json.dumps(decision))


def _ledger():
    return FakeLedger([{"id": "t1", "lane": "eng"}, {"id": "t2", "lane": "eng"}, {"id": "t3", "lane": "ci"}])


def _workers(runtime):
    return [task for _, _, task in runtime.spawned]


def test_manual_caps_above_the_host_room_spawn_up_to_the_room_and_the_rest_once_room_returns(store):
    ledger, runtime = _ledger(), FakeRuntime()
    _decide(store, 2)
    actions = tick.tick("sw", store, ledger, runtime, now_ms=1_000)
    held = f"holding spawns: host memory, room 2 is used: {MEMORY}"
    assert [name for name, _ in runtime.masters] == ["master@a1b2c3-0001"]
    assert _workers(runtime) == ["t1"]
    assert held in actions
    assert tick.spawn_holds(store, "sw") == [held]
    _decide(store, 3)
    actions = tick.tick("sw", store, ledger, runtime, now_ms=61_000)
    assert _workers(runtime) == ["t1", "t3", "t2"]
    assert not any(action.startswith("holding spawns") for action in actions)
    assert tick.spawn_holds(store, "sw") == []


def test_an_unknown_host_lets_every_spawn_pass(store):
    ledger, runtime = _ledger(), FakeRuntime()
    _decide(store, None, UNKNOWN, "unknown")
    actions = tick.tick("sw", store, ledger, runtime, now_ms=1_000)
    assert _workers(runtime) == ["t1", "t3", "t2"]
    assert not any(action.startswith("holding") for action in actions)


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
    assert f"holding the master spawn: host load, room 0 is used: {reason}" in actions
    assert runtime.masters == []
    assert [a for a in store.agents("sw") if a.lane == MASTER] == []
    _decide(store, 1)
    actions = tick.tick("sw", store, FakeLedger([]), runtime, now_ms=61_000)
    assert "spawned master master@a1b2c3-0001" in actions


def test_the_quota_seat_check_runs_before_the_host_gate(store):
    _decide(store, 0)
    actions = tick.tick("sw", store, _ledger(), FakeRuntime(full=True), now_ms=1_000)
    assert "no session slot for the master, waiting" in actions
    assert not any(action.startswith("holding") for action in actions)


def test_swarm_status_names_the_host_room_and_the_held_spawn(store, monkeypatch, capsys):
    ledger = _ledger()
    _decide(store, 2)
    tick.tick("sw", store, ledger, FakeRuntime(), now_ms=1_000)
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setattr(cli, "LedgerClient", lambda: ledger)
    assert cli.main(["sw", "status"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert f"host room 2: {MEMORY}" in lines
    assert f"holding spawns: host memory, room 2 is used: {MEMORY}" in lines


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
