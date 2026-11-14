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
from scripts.hive import execution_events, execution_stop
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
    running = replace(agent, state="working", pane_id="w1:p1", launched_at=NOW)
    store.put_agent(SLUG, running)
    rt.live.add(agent.name)
    event = start_event(running, readiness.generation(ledger.rows["t1"]))
    execution_events.started(bus, event)
    claims.Claims(store).handle(event)
    assert store.claims(SLUG, "t1") == 1
    assert reconciler.legacy_agents(store, SLUG) == []
    return running, event


@pytest.mark.feature("events-retirement-refunds-claim-life")
def test_a_swarm_stop_refunds_the_claim_life_of_an_events_launched_agent(
    env: tuple[RedisStore, Any, Any], bus: Bus, seat: Callable[..., AgentRecord]
) -> None:
    store, ledger, rt = env
    agent, _ = launched(env, bus, seat)

    stop_now(store, SLUG, rt, ledger)

    assert agent.name in rt.killed
    assert [seated.name for seated in store.agents(SLUG)] == []
    assert store.claims(SLUG, "t1") == 0
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["claimed_by"]) == ("open", "")


def retire(ending: str, env: tuple[RedisStore, Any, Any], agent: AgentRecord) -> None:
    """Retires ``agent`` the way the swarm does for ``ending``: an account mismatch or idling."""
    store, ledger, rt = env
    rows = {row["id"]: row for row in ledger.tasks(SLUG)}
    if ending == "mismatch":
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
@pytest.mark.parametrize("ending", ["mismatch", "idle"])
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


@pytest.mark.feature("events-retirement-stops-the-execution")
@pytest.mark.parametrize("ending", ["mismatch", "idle"])
def test_a_swarm_retirement_sends_the_stop_to_the_hive_that_started_the_agent(
    env: tuple[RedisStore, Any, Any], bus: Bus, seat: Callable[..., AgentRecord], ending: str
) -> None:
    agent, event = launched(env, bus, seat)

    retire(ending, env, agent)

    events = bus.for_swarm(SLUG).events
    stops = events.consumer(execution_stop.durable(SLUG, agent.hive), execution_stop.stop_subject(SLUG, agent.hive))
    sent = [delivery.event for delivery in stops.fetch(execution_stop.BATCH, 0)]
    assert [(stop.type, stop.key, stop.cause) for stop in sent] == [(execution_stop.TYPE, agent.execution_id, event.id)]
    assert sent[0].data["reason"] == ending


FREES = [
    (agent_steps._free_revoked, "revoked"),
    (agent_steps._free_refused, "refused"),
    (agent_steps._free_stuck, "stuck"),
]


def sent_stops(bus: Bus, agent: AgentRecord) -> list[Envelope]:
    """The execution stops the hub sent the hive that runs ``agent``."""
    events = bus.for_swarm(SLUG).events
    stops = events.consumer(execution_stop.durable(SLUG, agent.hive), execution_stop.stop_subject(SLUG, agent.hive))
    return [delivery.event for delivery in stops.fetch(execution_stop.BATCH, 0)]


@pytest.mark.feature("events-freed-agent-stops-the-execution")
@pytest.mark.parametrize(("free", "reason"), FREES)
def test_a_freed_events_agent_gets_one_stop_sent_to_the_hive_that_started_it(
    env: tuple[RedisStore, Any, Any], bus: Bus, seat: Callable[..., AgentRecord], free: Callable[..., Any], reason: str
) -> None:
    store, ledger, rt = env
    agent, event = launched(env, bus, seat)
    rows = {row["id"]: row for row in ledger.tasks(SLUG)}

    free(SLUG, store, ledger, rt, rows, agent, NOW + 1)

    sent = sent_stops(bus, agent)
    assert [(stop.type, stop.key, stop.cause) for stop in sent] == [(execution_stop.TYPE, agent.execution_id, event.id)]
    assert sent[0].data["reason"] == reason
    assert [seated.name for seated in store.agents(SLUG)] == []


@pytest.mark.feature("events-freed-agent-stops-the-execution")
@pytest.mark.parametrize(("free", "reason"), FREES)
def test_a_freed_legacy_agent_sends_no_stop_and_is_still_retired(
    env: tuple[RedisStore, Any, Any], bus: Bus, seat: Callable[..., AgentRecord], free: Callable[..., Any], reason: str
) -> None:
    store, ledger, rt = env
    assert run(SLUG, "create", "--repo", "/repo") == 0
    agent = replace(seat("t1"), state="working", pane_id="w1:p1", launched_at=NOW)
    store.put_agent(SLUG, agent)
    rt.live.add(agent.name)
    rows = {row["id"]: row for row in ledger.tasks(SLUG)}

    free(SLUG, store, ledger, rt, rows, agent, NOW + 1)

    assert agent.name in rt.killed
    assert sent_stops(bus, agent) == []
    assert [seated.name for seated in store.agents(SLUG)] == []


def handed_off(
    env: tuple[RedisStore, Any, Any], bus: Bus, seat: Callable[..., AgentRecord], cause: str
) -> tuple[AgentRecord, Envelope]:
    """Runs an agent on t1 that then ends its life for ``cause``: a quota handoff, or a park on the open task t2.
    The hive spoke still holds its started claim for the hub lifecycle to settle on exit."""
    store, ledger, _ = env
    agent, event = launched(env, bus, seat)
    if cause == "quota":
        store.put_handoff(SLUG, "t1", "continue here", seat=agent.seat, envelope={"reason": "quota"})
    else:
        ledger.rows["t1"]["parked_on"] = ["t2"]
    bus.for_swarm(SLUG).state("spoke-work").create(
        "claim", json.dumps({"state": "started", "claim_revision": 1}).encode()
    )
    return agent, event


def exited(env: tuple[RedisStore, Any, Any], bus: Bus, event: Envelope) -> None:
    """Hands the hub lifecycle reactor the hive's execution.exited event for the start ``event``."""
    store, ledger, _ = env
    react = lifecycle.Lifecycle(bus, store, ledger, release=lambda _store, _slug, _agent: None)
    react.handle(replace(event, id=f"exited:{event.key}", type="execution.exited"))


@pytest.mark.feature("events-parked-retirement-after-release-refunds-nothing")
@pytest.mark.parametrize("cause", ["quota", "parked"])
def test_an_events_parked_retirement_refunds_nothing_once_its_claim_was_released(
    env: tuple[RedisStore, Any, Any], bus: Bus, seat: Callable[..., AgentRecord], cause: str
) -> None:
    store, _, _ = env
    agent, event = handed_off(env, bus, seat, cause)
    assert store.release(SLUG, "t1", agent.name)

    exited(env, bus, event)

    assert [seated.name for seated in store.agents(SLUG)] == []
    assert store.claims(SLUG, "t1") == 1


@pytest.mark.feature("events-parked-retirement-after-release-refunds-nothing")
@pytest.mark.parametrize("cause", ["quota", "parked"])
def test_an_events_parked_retirement_still_holding_its_claim_refunds_its_life(
    env: tuple[RedisStore, Any, Any], bus: Bus, seat: Callable[..., AgentRecord], cause: str
) -> None:
    store, _, _ = env
    _, event = handed_off(env, bus, seat, cause)

    exited(env, bus, event)

    assert [seated.name for seated in store.agents(SLUG)] == []
    assert store.claims(SLUG, "t1") == 0
