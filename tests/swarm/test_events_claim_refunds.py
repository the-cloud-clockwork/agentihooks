"""Claim life refunds when the swarm retires an agent the events path launched, seated through hub admission."""

import json
from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.events.bus import Bus
from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.events.memory import open_bus
from scripts.hive import execution_events
from scripts.hub import claims, lifecycle, reconciler
from scripts.swarm import launch_check, readiness
from scripts.swarm.cli import stop_now
from scripts.swarm.store import AgentRecord, RedisStore
from scripts.swarm.tick import IDLE_KILL_TICKS
from scripts.tick.steps import agents as agent_steps
from scripts.tick.steps import observe
from tests.helpers.seat import seat
from tests.swarm.test_cli import env, run

pytestmark = pytest.mark.unit
_fixtures = (env, seat)

SLUG = "sw"
NOW = 1_000_000


class KeptBus:
    """The test bus as ``reconciler.open_bus`` hands it out: closing it keeps the shared memory bus open."""

    def __init__(self, bus: Any) -> None:
        self.bus = bus

    def for_swarm(self, slug: str) -> Any:
        """The shared bus scoped to ``slug``."""
        return self.bus.for_swarm(slug)

    def close(self) -> None:
        """Keeps the shared bus open for the next caller."""


@pytest.fixture
def bus(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A memory bus the hub reconciler reads as a real transport, so an agent with a recorded start is events owned."""
    shared = open_bus(BusConfig())
    monkeypatch.setattr(reconciler.BusConfig, "from_environ", lambda: SimpleNamespace(backend="nats"))
    monkeypatch.setattr(reconciler, "open_bus", lambda _config: KeptBus(shared))
    yield shared
    shared.close()


def start_event(agent: AgentRecord, generation: int) -> Envelope:
    """The execution.started event the hive publishes for ``agent`` on task generation ``generation``."""
    return Envelope(
        id=f"started:{agent.execution_id}",
        type="execution.started",
        swarm=SLUG,
        entity="execution",
        key=agent.execution_id,
        version=agent.generation,
        time=NOW,
        author=agent.hive,
        data={
            "task": agent.task,
            "name": agent.name,
            "hive": agent.hive,
            "claim_key": "claim",
            "claim_revision": 1,
            "task_generation": generation,
        },
    )


def launched(
    env: tuple[RedisStore, Any, Any], bus: Bus, seat: Callable[..., AgentRecord]
) -> tuple[AgentRecord, Envelope]:
    """Seats an agent on t1 and runs it as its hive would: the launch check passed, the pane works, the start is
    recorded on the bus and the hub counted its claim life. Returns the running record and its start event."""
    store, ledger, rt = env
    assert run(SLUG, "create", "--repo", "/repo") == 0
    agent = seat("t1")
    launch_check.forget(store, SLUG, agent.name)
    running = replace(agent, state="working", pane_id="w1:p1", started_at=NOW, launched_at=NOW)
    store.put_agent(SLUG, running)
    rt.live.add(agent.name)
    event = start_event(running, readiness.generation(ledger.rows["t1"]))
    execution_events.started(bus, event)
    claims.Claims(store).handle(event)
    assert store.claims(SLUG, "t1") == 1
    return running, event


def retire(ending: str, env: tuple[RedisStore, Any, Any], agent: AgentRecord) -> None:
    """Retires ``agent`` the way the swarm does for ``ending``: an account mismatch, idling or a swarm stop."""
    store, ledger, rt = env
    rows = {row["id"]: row for row in ledger.tasks(SLUG)}
    if ending == "stop":
        stop_now(store, SLUG, rt, ledger)
    elif ending == "mismatch":
        store.put_agent(SLUG, replace(agent, account="old-account"))
        original = rt.bindings
        rt.bindings = lambda agents: {
            name: {**facts, "account": "new-account"} for name, facts in original(agents).items()
        }
        observe._verify(SLUG, store, ledger, rt, rows, NOW + 1)
    else:
        rt.statuses[agent.name] = "idle"
        store.put_agent(SLUG, replace(agent, idle_ticks=IDLE_KILL_TICKS - 1))
        agent_steps._reap(SLUG, store, ledger, rt, rows, NOW + 1)


@pytest.mark.feature("events-retirement-refunds-claim-life")
@pytest.mark.parametrize("ending", ["mismatch", "idle", "stop"])
def test_a_swarm_retirement_refunds_the_claim_life_of_an_events_launched_agent(
    env: tuple[RedisStore, Any, Any], bus: Bus, seat: Callable[..., AgentRecord], ending: str
) -> None:
    store, ledger, rt = env
    agent, _ = launched(env, bus, seat)

    retire(ending, env, agent)

    assert agent.name in rt.killed
    assert [seated.name for seated in store.agents(SLUG)] == []
    assert store.claims(SLUG, "t1") == 0
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["claimed_by"]) == ("open", "")


@pytest.mark.feature("events-parked-retirement-after-release-refunds-nothing")
@pytest.mark.parametrize("released, expected_claims", [(True, 1), (False, 0)])
def test_an_events_exit_of_a_quota_handoff_refunds_nothing_once_its_claim_was_released(
    env: tuple[RedisStore, Any, Any], bus: Bus, seat: Callable[..., AgentRecord], released: bool, expected_claims: int
) -> None:
    store, ledger, _ = env
    agent, event = launched(env, bus, seat)
    store.put_handoff(SLUG, "t1", "continue here", seat=agent.seat, envelope={"reason": "quota"})
    if released:
        assert store.release(SLUG, "t1", agent.name)
    bus.for_swarm(SLUG).state("spoke-work").create(
        "claim", json.dumps({"state": "started", "claim_revision": 1}).encode()
    )
    react = lifecycle.Lifecycle(bus, store, ledger, release=lambda _store, _slug, _agent: None)

    react.handle(replace(event, id=f"exited:{agent.execution_id}", type="execution.exited"))

    assert [seated.name for seated in store.agents(SLUG)] == []
    assert store.claims(SLUG, "t1") == expected_claims
