"""The reap step retires agent records whose execution ended out of the tick's reach, releasing what they hold."""

from dataclasses import replace
from typing import Any

import pytest

from scripts.hive import reports
from scripts.swarm import tick
from scripts.swarm.pane import PaneObservation
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_v2.kubernetes.failures import PENDING_POD
from scripts.tick.steps import agents

pytestmark = pytest.mark.unit

SLUG = "sw"
NOW = 1_800_000_000_000
LATER = NOW + reports.LOST_MS


class _Ledger:
    """A ledger that reopens every task it is asked to update."""

    def __init__(self) -> None:
        self.updates: list[tuple[str, dict[str, Any]]] = []

    def update_task(self, slug: str, task_id: str, fields: dict[str, Any], **guards: Any) -> dict[str, Any]:
        """Record the update and answer the task reopened."""
        self.updates.append((task_id, fields))
        return {"state": "open", "claimed_by": ""}

    def task(self, slug: str, task_id: str) -> dict[str, Any]:
        """Answer no live row."""
        return {}


class _Unreachable:
    """A runtime that cannot observe or retire anything."""

    def live_names(self) -> set[str]:
        """Answer no live process."""
        return set()

    def observe(self, agent: AgentRecord) -> PaneObservation:
        """Answer that the runtime did not see the agent."""
        return PaneObservation("unknown")

    def retire(self, agent: AgentRecord, homes: Any = ()) -> bool:
        """Refuse every retire."""
        return False

    def refusal(self, agent: AgentRecord) -> dict[str, Any]:
        """Name why the retire was refused."""
        return {"process": 0, "refusal": "no herdr server is running"}

    def reported(self, agent: AgentRecord) -> bool:
        """Answer no hive report for the agent."""
        return False


@pytest.fixture
def store() -> RedisStore:
    """Return a fakeredis swarm store for one running swarm."""
    import fakeredis

    result = RedisStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
    result.create(SwarmConfig(SLUG, ".", 2, 0, state="running"))
    return result


def _unbound(store: RedisStore, task: str) -> AgentRecord:
    """Start a Kubernetes replacement attempt whose pod never bound, suspect and holding ``task``."""
    target = {"pod_namespace": "workers", "pod_name": PENDING_POD}
    name = store.next_name(SLUG, "eng")
    record = AgentRecord(name, "eng", task, seat=f"eng-1@{SLUG}", runtime_backend="kubernetes", runtime_target=target)
    agent = replace(store.start_execution(SLUG, record), state=tick.SUSPECT)
    store.put_agent(SLUG, agent)
    assert store.claim(SLUG, task, name, tick.LEASE_MS)
    return agent


def _reap(store: RedisStore, ledger: _Ledger, rows: dict[str, dict[str, Any]], agent: AgentRecord, now: int) -> Any:
    """Judge one agent in a reap pass at ``now``."""
    judging = tick._Reaping(SLUG, store, ledger, _Unreachable(), rows, set(), now)
    return agents._reap_agent(judging, agent)


@pytest.mark.feature("swarm-tick-reap-ended-executions")
def test_an_attempt_whose_pod_never_bound_is_retired_and_releases_its_task(store: RedisStore) -> None:
    agent = _unbound(store, "t1")
    rows = {"t1": {"state": "claimed", "claimed_by": agent.name, "claim_generation": 1}}
    ledger = _Ledger()

    assert _reap(store, ledger, rows, agent, NOW) == []
    assert store.claimant(SLUG, "t1") == agent.name

    actions = _reap(store, ledger, rows, agent, LATER)

    assert actions == [f"retired {agent.name}: its pod never started, task t1 reopened"]
    assert store.claimant(SLUG, "t1") is None
    assert agent.name not in {a.name for a in store.agents(SLUG)}
    assert rows["t1"]["state"] == "open"
    assert not store.redis.hexists(store.key(SLUG, "unbound-since"), agent.name)


@pytest.mark.feature("swarm-tick-reap-ended-executions")
def test_the_unbound_window_holds_across_passes_until_the_store_retires_the_agent(store: RedisStore) -> None:
    agent = _unbound(store, "t1")

    assert agents._remote_gone_reason(SLUG, store, _Unreachable(), agent, NOW) == ""
    assert agents._remote_gone_reason(SLUG, store, _Unreachable(), agent, LATER) == "its pod never started"
    assert agents._remote_gone_reason(SLUG, store, _Unreachable(), agent, LATER + 1) == "its pod never started"
    assert store.drop_agent(SLUG, agent.name)
    assert not store.redis.hexists(store.key(SLUG, "unbound-since"), agent.name)


@pytest.mark.feature("swarm-tick-reap-ended-executions")
def test_an_attempt_whose_pod_never_bound_keeps_its_task_inside_the_lost_window(store: RedisStore) -> None:
    agent = _unbound(store, "t1")
    rows = {"t1": {"state": "claimed", "claimed_by": agent.name, "claim_generation": 1}}

    assert _reap(store, _Ledger(), rows, agent, NOW) == []
    assert _reap(store, _Ledger(), rows, agent, LATER - 1) == []
    assert store.claimant(SLUG, "t1") == agent.name


@pytest.mark.feature("swarm-tick-reap-ended-executions")
def test_a_bound_pod_is_never_judged_by_the_unbound_window(store: RedisStore) -> None:
    agent = _unbound(store, "t1")
    bound = replace(agent, runtime_target={**agent.runtime_target, "pod_name": "swarm-t1"})

    assert agents._remote_gone_reason(SLUG, store, _Unreachable(), bound, NOW) == ""
    assert agents._remote_gone_reason(SLUG, store, _Unreachable(), bound, LATER) == ""
    assert agents._remote_gone_reason(SLUG, store, _Unreachable(), replace(agent, state="working"), LATER) == ""
    assert store.claimant(SLUG, "t1") == agent.name


@pytest.mark.feature("swarm-tick-reap-ended-executions")
def test_a_finished_master_retires_after_its_handoff_when_its_pane_is_out_of_reach(store: RedisStore) -> None:
    seat = f"{MASTER}@{SLUG}"
    old = AgentRecord("master@1-0640", MASTER, MASTER, seat=seat, state="finished", started_at=1, pane_id="w:p1")
    successor = AgentRecord("master@1-0688", MASTER, MASTER, seat=seat, state="working", started_at=2, pane_id="w:p2")
    store.put_agent(SLUG, old)
    store.put_agent(SLUG, successor)

    assert _reap(store, _Ledger(), {}, old, NOW) == [f"could not retire {old.name}, retrying next tick"]

    assert _reap(store, _Ledger(), {}, old, LATER) == [f"retired {old.name}"]
    assert {a.name for a in store.agents(SLUG)} == {successor.name}


@pytest.mark.feature("swarm-tick-reap-ended-executions")
def test_a_finished_master_with_no_successor_keeps_retrying(store: RedisStore) -> None:
    seat = f"{MASTER}@{SLUG}"
    old = AgentRecord("master@1-0640", MASTER, MASTER, seat=seat, state="finished", started_at=1, pane_id="w:p1")
    store.put_agent(SLUG, old)

    assert _reap(store, _Ledger(), {}, old, NOW) == [f"could not retire {old.name}, retrying next tick"]
    assert _reap(store, _Ledger(), {}, old, LATER) == [f"could not retire {old.name}, retrying next tick"]
    assert old.name in {a.name for a in store.agents(SLUG)}


def _failed_since(store: RedisStore, name: str) -> None:
    """Record a refused retire for ``name`` that began at ``NOW``."""
    tick.retire_watch.failed(store, SLUG, name, {"process": 0, "refusal": "no herdr server is running"}, NOW)


@pytest.mark.feature("swarm-tick-reap-ended-executions")
def test_only_a_finished_master_counts_as_handed_on(store: RedisStore) -> None:
    seat = f"{MASTER}@{SLUG}"
    working = AgentRecord("master@1-0640", MASTER, MASTER, seat=seat, state="working", started_at=1)
    successor = AgentRecord("master@1-0688", MASTER, MASTER, seat=seat, state="working", started_at=2)
    store.put_agent(SLUG, working)
    store.put_agent(SLUG, successor)
    _failed_since(store, working.name)

    assert not agents._handed_on(SLUG, store, working, LATER)
    assert agents._handed_on(SLUG, store, replace(working, state="finished"), LATER)


@pytest.mark.feature("swarm-tick-reap-ended-executions")
def test_a_later_agent_in_another_seat_does_not_hand_a_master_on(store: RedisStore) -> None:
    old = AgentRecord("master@1-0640", MASTER, MASTER, seat=f"{MASTER}@{SLUG}", state="finished", started_at=1)
    engineer = AgentRecord("eng@1-0700", "eng", "t1", seat=f"eng-1@{SLUG}", state="working", started_at=2)
    store.put_agent(SLUG, old)
    store.put_agent(SLUG, engineer)
    _failed_since(store, old.name)

    assert not agents._handed_on(SLUG, store, old, LATER)


@pytest.mark.feature("swarm-tick-reap-ended-executions")
def test_a_seat_peer_started_at_the_same_time_does_not_hand_a_master_on(store: RedisStore) -> None:
    seat = f"{MASTER}@{SLUG}"
    old = AgentRecord("master@1-0640", MASTER, MASTER, seat=seat, state="finished", started_at=1)
    peer = AgentRecord("master@1-0641", MASTER, MASTER, seat=seat, state="working", started_at=1)
    store.put_agent(SLUG, old)
    store.put_agent(SLUG, peer)
    _failed_since(store, old.name)

    assert not agents._handed_on(SLUG, store, old, LATER)
