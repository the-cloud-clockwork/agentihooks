import json

import pytest

from scripts.swarm import capacity, cli, host_budget, tick
from scripts.swarm.store import RedisStore, SwarmConfig
from tests.swarm.test_tick import FakeLedger, FakeRuntime

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


@pytest.mark.parametrize(("hive", "spent"), [("", 1), ("workstation-root", 1), ("remote", 0)])
def test_root_hive_launches_spend_workstation_room_while_remote_hives_do_not(store, monkeypatch, hive, spent):
    from scripts.swarm import commands
    from scripts.swarm.store import AgentRecord

    monkeypatch.setattr(commands, "hive_id", lambda: "workstation-root")
    agent = AgentRecord("codex-worker", "eng", "task", harness="codex", hive=hive)
    tick._spend_local(store, agent, 1000)
    assert tick.host_spent(store, 1000, 1000) == spent
    assert store.redis.zrange(tick.HOST_SPENDS, 0, -1) == (["codex-worker"] if spent else [])


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
