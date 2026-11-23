"""Claim life refunds when the swarm retires an agent the events path launched, seated through hub admission."""

import json
from collections.abc import Callable
from dataclasses import asdict, replace
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from scripts.events.bus import Bus
from scripts.events.config import BIND_ENV, BusConfig
from scripts.events.envelope import Envelope
from scripts.events.memory import open_bus
from scripts.hive import execution_events, execution_stop
from scripts.hub import claims, lifecycle, reconciler
from scripts.hub import exits as hub_exits
from scripts.swarm import idle, launch_check, readiness
from scripts.swarm import tick as tick_module
from scripts.swarm.cli import stop_now
from scripts.swarm.store import AgentRecord, RedisStore
from scripts.swarm.tick import IDLE_KILL_TICKS
from scripts.swarm_v2.runtime.base import Outcome, Status
from scripts.tick.steps import agents as agent_steps
from scripts.tick.steps import observe
from scripts.tick.steps.common import DROP_REFUSED
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


def spoke_started(bus: Bus) -> None:
    """Records the started claim the hive spoke holds for the running execution, so a stop finds it not yet ended."""
    bus.for_swarm(SLUG).state("spoke-work").create(
        "claim", json.dumps({"state": "started", "claim_revision": 1}).encode()
    )


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
    spoke_started(bus)

    retire(ending, env, agent)

    sent = sent_stops(bus, agent)
    assert [(stop.type, stop.key, stop.cause) for stop in sent] == [(execution_stop.TYPE, agent.execution_id, event.id)]
    assert sent[0].data["reason"] == ending


@pytest.mark.feature("events-retirement-stops-the-execution")
@pytest.mark.parametrize(
    ("free", "reason"),
    [
        (agent_steps._free_refused, "refused"),
        (agent_steps._free_revoked, "revoked"),
        (agent_steps._free_stuck, "stuck"),
    ],
)
def test_a_watch_retirement_refunds_and_sends_the_stop_to_the_hive_that_started_the_agent(
    env: tuple[RedisStore, Any, Any],
    bus: Bus,
    seat: Callable[..., AgentRecord],
    free: Callable[..., list[str]],
    reason: str,
) -> None:
    store, ledger, rt = env
    agent, event = launched(env, bus, seat)
    spoke_started(bus)

    free(SLUG, store, ledger, rt, {row["id"]: row for row in ledger.tasks(SLUG)}, agent, NOW + 1)

    assert agent.name in rt.killed
    assert store.claims(SLUG, "t1") == 0
    sent = sent_stops(bus, agent)
    assert [(stop.key, stop.cause, stop.data["reason"]) for stop in sent] == [(agent.execution_id, event.id, reason)]


def sent_stops(bus: Bus, agent: AgentRecord) -> list[Envelope]:
    """The stops waiting for the hive that ``agent`` names."""
    stops = bus.for_swarm(SLUG).stops.consumer(
        execution_stop.durable(SLUG, agent.hive), execution_stop.stop_subject(SLUG, agent.hive), execution_stop.POLICY
    )
    return [delivery.event for delivery in stops.fetch(execution_stop.BATCH, 0)]


@pytest.mark.feature("events-retirement-stops-the-execution")
@pytest.mark.parametrize(("dropped", "stops"), [(DROP_REFUSED, 0), ("", 1)])
def test_the_hive_stop_waits_for_the_store_to_take_the_drop(
    env: tuple[RedisStore, Any, Any], bus: Bus, seat: Callable[..., AgentRecord], dropped: str, stops: int
) -> None:
    store, _, _ = env
    agent, _ = launched(env, bus, seat)
    spoke_started(bus)

    assert tick_module._stop_hive(SLUG, store, agent, "idle", dropped) == dropped

    assert len(sent_stops(bus, agent)) == stops


@pytest.mark.feature("events-retirement-stops-the-execution")
@pytest.mark.parametrize("ending", ["mismatch", "idle"])
def test_a_retirement_whose_drop_the_store_refused_sends_no_stop(
    env: tuple[RedisStore, Any, Any],
    bus: Bus,
    seat: Callable[..., AgentRecord],
    monkeypatch: pytest.MonkeyPatch,
    ending: str,
) -> None:
    store, _, _ = env
    agent, _ = launched(env, bus, seat)
    spoke_started(bus)
    monkeypatch.setattr(store, "drop_agent", lambda *_args, **_kwargs: False)

    retire(ending, env, agent)

    assert sent_stops(bus, agent) == []


@pytest.mark.feature("events-retirement-stops-the-execution")
def test_a_legacy_agent_retirement_sends_no_stop(
    env: tuple[RedisStore, Any, Any], bus: Bus, seat: Callable[..., AgentRecord]
) -> None:
    store, _, _ = env
    assert run(SLUG, "create", "--repo", "/repo") == 0
    agent = seat("t1")
    assert reconciler.legacy_agents(store, SLUG) == [agent]

    assert reconciler.stop_admitted(SLUG, agent, "idle") == execution_stop.UNKNOWN

    assert sent_stops(bus, agent) == []


@pytest.mark.feature("events-legacy-free-sends-no-stop")
@pytest.mark.parametrize(
    "free",
    [agent_steps._free_revoked, agent_steps._free_refused, agent_steps._free_stuck],
    ids=["revoked", "refused", "stuck"],
)
def test_a_freed_legacy_agent_sends_no_stop_and_is_still_retired(
    env: tuple[RedisStore, Any, Any], bus: Bus, seat: Callable[..., AgentRecord], free: Callable[..., list[str]]
) -> None:
    store, ledger, rt = env
    assert run(SLUG, "create", "--repo", "/repo") == 0
    agent = replace(seat("t1"), state="working", pane_id="w1:p1", launched_at=NOW)
    store.put_agent(SLUG, agent)
    rt.live.add(agent.name)
    assert [legacy.name for legacy in reconciler.legacy_agents(store, SLUG)] == [agent.name]

    free(SLUG, store, ledger, rt, {row["id"]: row for row in ledger.tasks(SLUG)}, agent, NOW + 1)

    assert agent.name in rt.killed
    assert [seated.name for seated in store.agents(SLUG)] == []
    assert sent_stops(bus, agent) == []


@pytest.mark.feature("events-retirement-stops-the-execution")
def test_the_hub_stop_opens_no_bus_on_the_memory_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(_config: Any) -> Any:
        raise AssertionError("the memory bus records no start, so no bus is opened")

    monkeypatch.setattr(reconciler.BusConfig, "from_environ", lambda: SimpleNamespace(backend="memory"))
    monkeypatch.setattr(reconciler, "open_bus", unexpected)

    assert reconciler.stop_admitted(SLUG, AgentRecord(name="a1", lane="eng", task="t1"), "idle") == (
        execution_stop.UNKNOWN
    )


@pytest.mark.feature("events-retirement-stops-the-execution")
@pytest.mark.parametrize("refused", [False, True])
def test_the_hub_stop_reports_its_answer_on_stderr_and_closes_the_bus_it_opened(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], refused: bool
) -> None:
    config = SimpleNamespace(backend="nats")
    opened: list[Any] = []
    closed: list[bool] = []

    def open_for(given: Any) -> Any:
        opened.append(given)
        if refused:
            raise ConnectionError("bus down")
        return SimpleNamespace(close=lambda: closed.append(True))

    monkeypatch.setattr(reconciler.BusConfig, "from_environ", lambda: config)
    monkeypatch.setattr(reconciler, "open_bus", open_for)
    monkeypatch.setattr(reconciler.execution_stop, "stop", lambda _bus, _slug, _agent, _reason: execution_stop.SENT)
    agent = AgentRecord(name="a1", lane="eng", task="t1")

    outcome = reconciler.stop_admitted(SLUG, agent, "idle")

    expected = f"{execution_stop.FAILED} (ConnectionError: bus down)" if refused else execution_stop.SENT
    assert (outcome, opened, closed) == (expected, [config], [] if refused else [True])
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", f"hub execution stop for a1 in swarm {SLUG} after idle: {expected}\n")


@pytest.mark.feature("events-retirement-stops-the-execution")
def test_a_reachable_agent_is_retired_through_its_runtime_with_its_task_scratch_homes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retired: list[tuple[str, dict[str, Any]]] = []
    runtime = SimpleNamespace(retire=lambda agent, **kwargs: retired.append((agent.name, kwargs)) or True)
    monkeypatch.setattr(tick_module.reaper, "scratch_homes", lambda slug, task: [f"{slug}/{task}"])

    assert tick_module._retire_or_stop(SLUG, runtime, AgentRecord(name="a1", lane="eng", task="t1"), "idle", Mock())

    assert retired == [("a1", {"homes": [f"{SLUG}/t1"]})]


@pytest.mark.feature("events-retirement-stops-the-execution")
@pytest.mark.parametrize("ending", ["mismatch", "idle"])
def test_an_agent_its_runtime_cannot_reach_is_retired_through_the_hive_stop(
    env: tuple[RedisStore, Any, Any], bus: Bus, seat: Callable[..., AgentRecord], ending: str
) -> None:
    store, ledger, rt = env
    agent, event = launched(env, bus, seat)
    spoke_started(bus)
    rt.stuck.add(agent.name)

    retire(ending, env, agent)

    assert agent.name not in rt.killed
    assert [seated.name for seated in store.agents(SLUG)] == []
    assert store.claims(SLUG, "t1") == 0
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["claimed_by"]) == ("open", "")
    assert [(stop.key, stop.data["reason"]) for stop in sent_stops(bus, agent)] == [(agent.execution_id, ending)]


@pytest.mark.feature("events-retirement-stops-the-execution")
def test_an_unreachable_agent_is_stopped_before_its_drop_and_stays_seated_when_the_store_refuses_it(
    env: tuple[RedisStore, Any, Any], bus: Bus, seat: Callable[..., AgentRecord], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _, rt = env
    agent, event = launched(env, bus, seat)
    spoke_started(bus)
    rt.stuck.add(agent.name)
    monkeypatch.setattr(store, "drop_agent", lambda *_args, **_kwargs: False)

    retire("idle", env, agent)

    assert [(stop.key, stop.cause) for stop in sent_stops(bus, agent)] == [(agent.execution_id, event.id)]
    assert [seated.name for seated in store.agents(SLUG)] == [agent.name]


@pytest.mark.feature("events-retirement-stops-the-execution")
@pytest.mark.parametrize("hold", ["wait", "typed"])
def test_a_declared_wait_or_typed_input_holds_an_idle_admitted_agent_from_retirement(
    env: tuple[RedisStore, Any, Any], bus: Bus, seat: Callable[..., AgentRecord], hold: str
) -> None:
    store, _, rt = env
    agent, _ = launched(env, bus, seat)
    spoke_started(bus)
    if hold == "wait":
        idle.declare_wait(store.redis, SLUG, agent.name, NOW + 3_600_000, "checks", NOW)
    else:
        rt.typed[agent.name] = "a reply the operator is typing"

    retire("idle", env, agent)

    assert agent.name not in rt.killed
    assert [seated.name for seated in store.agents(SLUG)] == [agent.name]
    assert store.claims(SLUG, "t1") == 1
    assert sent_stops(bus, agent) == []


ENDED_STOP = "sent the stop for {name} to its hive: its work ended"


@pytest.mark.feature("events-retirement-refunds-claim-life")
@pytest.mark.parametrize(
    ("lane", "state", "live", "acknowledging", "ended", "judged"),
    [
        ("eng", "working", True, False, False, "watched"),
        ("eng", "starting", True, False, False, "watched"),
        ("master", "working", True, False, False, "watched"),
        ("eng", "working", False, False, False, ""),
        ("eng", "awaiting-decision", True, False, False, ""),
        ("eng", "retiring", True, False, False, ""),
        ("master", "starting", True, False, False, ""),
        ("eng", "working", True, True, False, ""),
        ("eng", "working", True, False, True, "stopped"),
        ("eng", "working", False, False, True, "stopped"),
        ("master", "finished", True, False, True, "stopped"),
        ("eng", "awaiting-decision", True, False, True, ""),
        ("eng", "retiring", True, False, True, ""),
        ("master", "starting", True, False, True, ""),
        ("eng", "working", True, True, True, ""),
    ],
)
def test_the_reap_step_judges_an_admitted_agent_by_its_pane_and_whether_its_work_ended(
    monkeypatch: pytest.MonkeyPatch,
    lane: str,
    state: str,
    live: bool,
    acknowledging: bool,
    ended: bool,
    judged: str,
) -> None:
    tick = agent_steps.tick
    agent = AgentRecord(name="a1", lane=tick.MASTER if lane == "master" else lane, task="t1", state=state)
    stops: list[tuple[str, str, str]] = []
    monkeypatch.setattr(tick, "_acknowledging", lambda *_args: acknowledging)
    monkeypatch.setattr(tick, "_ended", lambda *_args: ended)
    monkeypatch.setattr(tick, "_watch_agent", lambda *args: [f"watched {args[-2].name}"])
    monkeypatch.setattr(
        reconciler,
        "stop_admitted",
        lambda slug, stopped, reason: stops.append((slug, stopped.name, reason)) or execution_stop.SENT,
    )
    judging = SimpleNamespace(
        slug=SLUG, store=None, ledger=None, runtime=None, rows={}, live={"a1"} if live else set(), now_ms=NOW
    )

    actions = agent_steps._reap_admitted(cast(Any, judging), agent)

    expected = {"watched": ["watched a1"], "stopped": [ENDED_STOP.format(name="a1")], "": []}
    assert actions == expected[judged]
    assert stops == ([(SLUG, "a1", "ended")] if judged == "stopped" else [])


def finish(env: tuple[RedisStore, Any, Any], agent: AgentRecord, ending: str) -> None:
    """Ends the work of ``agent`` the way the swarm records it: its own done or ready marks it finished, or its task
    is done."""
    store, ledger, _ = env
    if ending == "finished":
        store.put_agent(SLUG, replace(agent, state="finished"))
    else:
        ledger.rows["t1"]["done"] = True


def reap_pass(env: tuple[RedisStore, Any, Any], now_ms: int) -> list[str]:
    """The actions of one agents reap pass over the ledger's current rows at ``now_ms``."""
    store, ledger, rt = env
    return agent_steps._reap(SLUG, store, ledger, rt, {row["id"]: row for row in ledger.tasks(SLUG)}, now_ms)


@pytest.mark.feature("events-ended-agent-stopped")
@pytest.mark.parametrize("live", [True, False])
@pytest.mark.parametrize("ending", ["finished", "task done"])
def test_an_admitted_agent_whose_work_ended_gets_the_stop_from_its_hive_in_one_reap_pass(
    env: tuple[RedisStore, Any, Any], bus: Bus, seat: Callable[..., AgentRecord], ending: str, live: bool
) -> None:
    store, _, rt = env
    agent, event = launched(env, bus, seat)
    spoke_started(bus)
    finish(env, agent, ending)
    if not live:
        rt.live.discard(agent.name)

    actions = reap_pass(env, NOW + 1)

    assert actions == [ENDED_STOP.format(name=agent.name)]
    sent = sent_stops(bus, agent)
    assert [(stop.key, stop.cause, stop.data["reason"]) for stop in sent] == [(agent.execution_id, event.id, "ended")]
    assert agent.name not in rt.killed
    assert [seated.name for seated in store.agents(SLUG)] == [agent.name]


@pytest.mark.feature("events-ended-agent-stopped")
def test_a_reap_pass_inside_the_stop_dedupe_window_sends_no_second_stop(
    env: tuple[RedisStore, Any, Any], bus: Bus, seat: Callable[..., AgentRecord]
) -> None:
    agent, _ = launched(env, bus, seat)
    spoke_started(bus)
    finish(env, agent, "finished")
    assert reap_pass(env, NOW + 1) == [ENDED_STOP.format(name=agent.name)]

    assert reap_pass(env, NOW + 2) == []

    assert [stop.key for stop in sent_stops(bus, agent)] == [agent.execution_id]


@pytest.mark.feature("events-ended-agent-stopped")
def test_an_ended_admitted_agent_whose_stop_the_bus_refused_is_stopped_on_the_next_pass(
    env: tuple[RedisStore, Any, Any], bus: Bus, seat: Callable[..., AgentRecord], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _ = launched(env, bus, seat)
    spoke_started(bus)
    finish(env, agent, "finished")
    working = execution_stop.stop
    monkeypatch.setattr(execution_stop, "stop", refused_stop)

    assert reap_pass(env, NOW + 1) == [f"could not send the stop for {agent.name}, retrying next tick: {REFUSED}"]

    monkeypatch.setattr(execution_stop, "stop", working)
    assert reap_pass(env, NOW + 2) == [ENDED_STOP.format(name=agent.name)]
    assert [stop.key for stop in sent_stops(bus, agent)] == [agent.execution_id]


@pytest.mark.feature("events-ended-agent-stopped")
def test_a_finished_admitted_agent_leaves_its_seat_and_launch_slot_once_its_hive_honors_the_stop(
    env: tuple[RedisStore, Any, Any], bus: Bus, seat: Callable[..., AgentRecord]
) -> None:
    store, ledger, _ = env
    agent, _ = launched(env, bus, seat)
    spoke_started(bus)
    finish(env, agent, "finished")
    hub_exits.serve(bus, lambda: store)
    hive = Mock()
    hive.find.return_value = hive.reap.return_value = Outcome("reap", Status.OK, "local", "pane")
    released: list[str] = []
    react = lifecycle.Lifecycle(bus, store, ledger, release=lambda _store, _slug, gone: released.append(gone.name))
    exits = bus.for_swarm(SLUG).events.consumer("audit-exits", f"swarm.{SLUG}.execution.exited")

    reap_pass(env, NOW + 1)
    assert execution_stop.Stopper(bus, SLUG, agent.hive, hive).poll() == [execution_stop.STOPPED]
    [reported] = [delivery.event for delivery in exits.fetch(10, 0)]
    react.handle(reported)

    hive.reap.assert_called_once_with(agent.name)
    assert reported.data["stop_reason"] == "ended"
    assert [seated.name for seated in store.agents(SLUG)] == []
    assert released == [agent.name]
    entry = bus.for_swarm(SLUG).state("spoke-work").get("claim")
    assert entry is not None
    assert json.loads(entry.value)["state"] == "exited"


def settle_claim(bus: Bus, claim: dict[str, Any] | None) -> None:
    """Leaves the spoke claim of the running execution as ``claim``, as the hub lifecycle, a later launch or a cleared
    claim leave it; no claim at all when ``claim`` is None."""
    if claim is not None:
        bus.for_swarm(SLUG).state("spoke-work").create("claim", json.dumps(claim).encode())


@pytest.mark.feature("events-ended-agent-dropped-once-settled")
@pytest.mark.parametrize(
    ("claim", "dropped"),
    [
        ({"state": "exited", "claim_revision": 1}, True),
        ({"state": "started", "claim_revision": 2}, True),
        (None, True),
        ({"state": "exiting", "claim_revision": 1}, False),
    ],
    ids=["exited", "another launch", "gone", "exiting"],
)
def test_an_admitted_agent_on_a_done_task_whose_stop_answers_ended_leaves_its_seat_once_the_lifecycle_cannot_settle_it(
    env: tuple[RedisStore, Any, Any],
    bus: Bus,
    seat: Callable[..., AgentRecord],
    claim: dict[str, Any] | None,
    dropped: bool,
) -> None:
    from scripts.swarm import local_slots

    store, _, rt = env
    agent, _ = launched(env, bus, seat)
    settle_claim(bus, claim)
    finish(env, agent, "task done")
    rt.live.discard(agent.name)
    slot = f"{SLUG}/{agent.name}"
    store.redis.hset(local_slots.key(agent.hive), slot, NOW)
    assert reconciler.stop_admitted(SLUG, agent, "ended") == execution_stop.ENDED

    actions = reap_pass(env, NOW + 1)

    assert actions == ([f"retired {agent.name}"] if dropped else [])
    assert [seated.name for seated in store.agents(SLUG)] == ([] if dropped else [agent.name])
    assert store.redis.hget(local_slots.key(agent.hive), slot) == (None if dropped else str(NOW))
    assert sent_stops(bus, agent) == []


@pytest.mark.feature("events-ended-agent-dropped-once-settled")
@pytest.mark.parametrize(
    ("outcome", "settled", "answer", "retired", "released"),
    [
        (execution_stop.ENDED, True, "retired a1", True, True),
        (execution_stop.UNKNOWN, True, "retired a1", True, True),
        (execution_stop.ENDED, True, f"kept a1{DROP_REFUSED}", True, False),
        (execution_stop.ENDED, True, "could not retire a1, retrying next tick", True, False),
        (execution_stop.ENDED, False, "retired a1", False, False),
        (execution_stop.UNKNOWN, False, "retired a1", False, False),
        (execution_stop.PENDING, True, "retired a1", False, False),
    ],
)
def test_an_ended_admitted_agent_is_retired_only_once_its_stop_answers_ended_or_unknown_and_its_claim_settled(
    monkeypatch: pytest.MonkeyPatch, outcome: str, settled: bool, answer: str, retired: bool, released: bool
) -> None:
    from scripts.hive import launch

    tick = agent_steps.tick
    agent = AgentRecord(name="a1", lane="eng", task="t1", state="working")
    asked: list[tuple[str, str]] = []
    calls: list[tuple[Any, ...]] = []
    returned: list[tuple[Any, ...]] = []
    monkeypatch.setattr(tick, "_acknowledging", lambda *_args: False)
    monkeypatch.setattr(tick, "_ended", lambda *_args: True)
    monkeypatch.setattr(reconciler, "stop_admitted", lambda _slug, _agent, _reason: outcome)
    monkeypatch.setattr(
        reconciler, "settled_admitted", lambda slug, checked: asked.append((slug, checked.name)) or settled
    )
    monkeypatch.setattr(tick, "_retire_ended", lambda *args: calls.append(args) or [answer])
    monkeypatch.setattr(launch, "release", lambda *args: returned.append(args))
    rows = {"t1": {"id": "t1", "done": True}}
    judging = SimpleNamespace(
        slug=SLUG, store="store", ledger="ledger", runtime="runtime", rows=rows, live=set(), now_ms=NOW
    )

    actions = agent_steps._reap_admitted(cast(Any, judging), agent)

    assert actions == ([answer] if retired else [])
    assert asked == ([] if outcome == execution_stop.PENDING else [(SLUG, "a1")])
    assert calls == ([(SLUG, "store", "ledger", "runtime", rows, agent, NOW)] if retired else [])
    assert returned == ([("store", SLUG, agent)] if released else [])


@pytest.mark.feature("events-ended-agent-dropped-once-settled")
def test_the_settled_check_opens_no_bus_on_the_memory_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(_config: Any) -> Any:
        raise AssertionError("the memory bus records no start, so no bus is opened")

    monkeypatch.setattr(reconciler.BusConfig, "from_environ", lambda: SimpleNamespace(backend="memory"))
    monkeypatch.setattr(reconciler, "open_bus", unexpected)

    assert reconciler.settled_admitted(SLUG, AgentRecord(name="a1", lane="eng", task="t1")) is False


@pytest.mark.feature("events-ended-agent-dropped-once-settled")
def test_the_settled_check_keeps_the_agent_and_says_so_when_the_bus_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refused(_config: Any) -> Any:
        raise ConnectionError("bus down")

    monkeypatch.setattr(reconciler.BusConfig, "from_environ", lambda: SimpleNamespace(backend="nats"))
    monkeypatch.setattr(reconciler, "open_bus", refused)

    assert reconciler.settled_admitted(SLUG, AgentRecord(name="a1", lane="eng", task="t1")) is False
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == (
        "",
        f"hub settled check for a1 in swarm {SLUG} failed (ConnectionError: bus down)\n",
    )


@pytest.mark.feature("events-ended-agent-dropped-once-settled")
@pytest.mark.parametrize(
    ("recorded", "lifecycle_done", "settled"),
    [("none", False, True), ("another execution", True, False), ("own", False, False), ("own", True, True)],
)
def test_the_settled_check_reads_the_start_its_hive_recorded_and_closes_the_bus(
    monkeypatch: pytest.MonkeyPatch, recorded: str, lifecycle_done: bool, settled: bool
) -> None:
    agent = AgentRecord(name="a1", lane="eng", task="t1", execution_id="exe-1", generation=2, hive="root")
    opened = SimpleNamespace(closed=[])
    opened.close = lambda: opened.closed.append(True)
    start = None if recorded == "none" else SimpleNamespace(key="exe-1" if recorded == "own" else "exe-other")
    asked: list[tuple[Any, ...]] = []
    monkeypatch.setattr(reconciler.BusConfig, "from_environ", lambda: SimpleNamespace(backend="nats"))
    monkeypatch.setattr(reconciler, "open_bus", lambda _config: opened)
    monkeypatch.setattr(reconciler.execution_stop, "started", lambda *args: asked.append(args) or start)
    monkeypatch.setattr(reconciler.execution_stop, "settled", lambda given, event: given is opened and lifecycle_done)

    assert reconciler.settled_admitted(SLUG, agent) is settled
    assert asked == [(opened, SLUG, "a1", 2)]
    assert opened.closed == [True]


@pytest.mark.feature("events-retirement-stops-the-execution")
@pytest.mark.parametrize("ending", ["mismatch", "idle"])
def test_the_hive_exit_after_a_tick_retirement_releases_the_launch_slot_and_settles_the_claim(
    env: tuple[RedisStore, Any, Any], bus: Bus, seat: Callable[..., AgentRecord], ending: str
) -> None:
    store, ledger, _ = env
    agent, event = launched(env, bus, seat)
    spoke_started(bus)
    retire(ending, env, agent)
    released: list[str] = []
    react = lifecycle.Lifecycle(bus, store, ledger, release=lambda _store, _slug, gone: released.append(gone.name))

    react.handle(replace(event, id=f"exited:{event.key}", type="execution.exited"))

    assert released == [agent.name]
    entry = bus.for_swarm(SLUG).state("spoke-work").get("claim")
    assert entry is not None
    assert json.loads(entry.value)["state"] == "exited"
    assert store.claims(SLUG, "t1") == 0


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


REFUSED = f"{execution_stop.FAILED} (ConnectionError: bus down)"


def refused_stop(_bus: Any, _slug: str, _agent: AgentRecord, _reason: str) -> str:
    """The execution stop while the bus refuses it: publishing raises."""
    raise ConnectionError("bus down")


@pytest.mark.feature("events-refused-stop-resent")
def test_a_stop_the_bus_refused_is_sent_again_on_each_reap_pass_until_the_bus_takes_it(
    env: tuple[RedisStore, Any, Any], bus: Bus, seat: Callable[..., AgentRecord], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, ledger, rt = env
    agent, event = launched(env, bus, seat)
    spoke_started(bus)
    working = execution_stop.stop
    monkeypatch.setattr(execution_stop, "stop", refused_stop)
    retire("idle", env, agent)
    assert [seated.name for seated in store.agents(SLUG)] == []
    rows = {row["id"]: row for row in ledger.tasks(SLUG)}

    refused = agent_steps._reap(SLUG, store, ledger, rt, rows, NOW + 2)
    monkeypatch.setattr(execution_stop, "stop", working)
    taken = agent_steps._reap(SLUG, store, ledger, rt, rows, NOW + 3)
    after = agent_steps._reap(SLUG, store, ledger, rt, rows, NOW + 4)

    assert refused == [f"sent the idle stop for {agent.name} again: {REFUSED}"]
    assert taken == [f"sent the idle stop for {agent.name} again: {execution_stop.SENT}"]
    assert after == []
    sent = [(stop.key, stop.cause, stop.data["reason"]) for stop in sent_stops(bus, agent)]
    assert sent == [(agent.execution_id, event.id, "idle")]


def pending_stops(store: RedisStore) -> dict[str, str]:
    """The stops of swarm ``sw`` waiting to be sent again, by execution id."""
    return cast("dict[str, str]", store.redis.hgetall(store.key(SLUG, reconciler.PENDING_STOPS)))


WATCH_FREES = {
    "refused": agent_steps._free_refused,
    "revoked": agent_steps._free_revoked,
    "stuck": agent_steps._free_stuck,
}


@pytest.mark.feature("events-refused-stop-resent")
@pytest.mark.parametrize("reason", ["refused", "revoked", "stuck", "mismatch", "idle"])
def test_every_retirement_keeps_the_stop_the_bus_refused_pending_for_its_execution(
    env: tuple[RedisStore, Any, Any],
    bus: Bus,
    seat: Callable[..., AgentRecord],
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
) -> None:
    store, ledger, rt = env
    agent, _ = launched(env, bus, seat)
    spoke_started(bus)
    monkeypatch.setattr(execution_stop, "stop", refused_stop)

    if reason in WATCH_FREES:
        WATCH_FREES[reason](SLUG, store, ledger, rt, {row["id"]: row for row in ledger.tasks(SLUG)}, agent, NOW + 1)
    else:
        retire(reason, env, agent)

    held = {execution: json.loads(raw) for execution, raw in pending_stops(store).items()}
    assert [seated.name for seated in store.agents(SLUG)] == []
    assert {execution: (stop["agent"]["name"], stop["reason"]) for execution, stop in held.items()} == {
        agent.execution_id: (agent.name, reason)
    }


@pytest.mark.feature("events-refused-stop-resent")
def test_a_refused_stop_for_an_agent_the_swarm_still_seats_is_discarded_and_not_sent_again(
    env: tuple[RedisStore, Any, Any], bus: Bus, seat: Callable[..., AgentRecord], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _, _ = env
    agent, event = launched(env, bus, seat)
    spoke_started(bus)
    seated = seat("t2")
    working = execution_stop.stop
    monkeypatch.setattr(execution_stop, "stop", refused_stop)
    assert reconciler.stop_dropped(store, SLUG, seated, "mismatch") == REFUSED
    assert reconciler.stop_dropped(store, SLUG, agent, "idle") == REFUSED
    assert store.drop_agent(SLUG, agent.name)
    monkeypatch.setattr(execution_stop, "stop", working)

    resent = reconciler.retry_stops(store, SLUG)

    assert resent == [f"sent the idle stop for {agent.name} again: {execution_stop.SENT}"]
    assert [(stop.key, stop.cause) for stop in sent_stops(bus, agent)] == [(agent.execution_id, event.id)]
    assert [record.name for record in store.agents(SLUG)] == [seated.name]
    assert pending_stops(store) == {}


@pytest.mark.feature("events-refused-stop-resent")
def test_the_reap_pass_sends_no_pending_stop_again_on_the_memory_backend(
    env: tuple[RedisStore, Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _, _ = env
    agent = AgentRecord(name="a1", lane="eng", task="t1", execution_id="x1")
    held = json.dumps({"agent": asdict(agent), "reason": "idle"})
    store.redis.hset(store.key(SLUG, reconciler.PENDING_STOPS), agent.execution_id, held)
    monkeypatch.setattr(reconciler.BusConfig, "from_environ", lambda: SimpleNamespace(backend="memory"))

    assert reconciler.retry_stops(store, SLUG) == []

    assert pending_stops(store) == {agent.execution_id: held}


@pytest.mark.feature("events-refused-stop-resent")
def test_a_pass_stops_sending_again_at_the_first_stop_the_bus_still_refuses(
    env: tuple[RedisStore, Any, Any], bus: Bus, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _, _ = env
    agents = [AgentRecord(name=f"a{n}", lane="eng", task="t1", execution_id=f"x{n}") for n in (1, 2)]
    working = execution_stop.stop
    monkeypatch.setattr(execution_stop, "stop", refused_stop)
    assert [reconciler.stop_dropped(store, SLUG, agent, "idle") for agent in agents] == [REFUSED, REFUSED]

    refused = reconciler.retry_stops(store, SLUG)
    still = sorted(pending_stops(store))
    monkeypatch.setattr(execution_stop, "stop", working)
    taken = reconciler.retry_stops(store, SLUG)

    assert refused == [f"sent the idle stop for a1 again: {REFUSED}"]
    assert still == ["x1", "x2"]
    assert taken == [f"sent the idle stop for a{n} again: {execution_stop.UNKNOWN}" for n in (1, 2)]
    assert pending_stops(store) == {}


@pytest.mark.feature("events-refused-stop-resent")
def test_a_refused_stop_for_an_agent_with_no_execution_is_not_kept(
    env: tuple[RedisStore, Any, Any], bus: Bus, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _, _ = env
    monkeypatch.setattr(execution_stop, "stop", refused_stop)

    assert reconciler.stop_dropped(store, SLUG, AgentRecord(name="a1", lane="eng", task="t1"), "idle") == REFUSED

    assert pending_stops(store) == {}


@pytest.mark.feature("events-refused-stop-resent")
def test_a_stop_the_bus_takes_for_a_dropped_agent_keeps_nothing_pending(
    env: tuple[RedisStore, Any, Any], bus: Bus, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _, _ = env
    monkeypatch.setattr(execution_stop, "stop", lambda _bus, _slug, _agent, _reason: execution_stop.SENT)
    agent = AgentRecord(name="a1", lane="eng", task="t1", execution_id="x1")

    assert reconciler.stop_dropped(store, SLUG, agent, "idle") == execution_stop.SENT

    assert pending_stops(store) == {}


@pytest.mark.feature("member-tick-stop-forwarded-by-hub")
@pytest.mark.parametrize("reason", ["ended", "idle", "mismatch", "refused", "revoked", "stuck"])
@pytest.mark.parametrize("refuse", [False, True])
def test_a_member_bind_tick_parks_its_stop_until_the_hub_sends_it(
    env: tuple[RedisStore, Any, Any],
    bus: Bus,
    seat: Callable[..., AgentRecord],
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    refuse: bool,
) -> None:
    store, ledger, rt = env
    agent, event = launched(env, bus, seat)
    spoke_started(bus)
    original = execution_stop.stop
    refusal = Mock(side_effect=[ConnectionError("controller stop refused"), None] if refuse else [None])

    def send(controller: Bus, slug: str, stopped: AgentRecord, why: str) -> str:
        if controller is not bus:
            raise PermissionError("member cannot publish execution stops")
        refusal()
        return original(controller, slug, stopped, why)

    sender = Mock(side_effect=send)
    monkeypatch.setattr(execution_stop, "stop", sender)
    monkeypatch.setenv(BIND_ENV, "1")
    if reason == "ended":
        finish(env, agent, "finished")
        reap_pass(env, NOW + 1)
    elif reason in WATCH_FREES:
        WATCH_FREES[reason](SLUG, store, ledger, rt, {row["id"]: row for row in ledger.tasks(SLUG)}, agent, NOW + 1)
    else:
        monkeypatch.setattr(rt, "retire", Mock(return_value=False))
        retire(reason, env, agent)
    reap_pass(env, NOW + 2)

    sender.assert_not_called()
    queued = pending_stops(store)
    held = json.loads(queued[agent.execution_id])
    assert (held["agent"]["execution_id"], held["agent"]["name"], held["reason"]) == (
        agent.execution_id,
        agent.name,
        reason,
    )
    assert [seated.name for seated in store.agents(SLUG)] == ([agent.name] if reason == "ended" else [])
    assert sent_stops(bus, agent) == []

    hub = reconciler.Reconciler(bus, store, clock=lambda: NOW + 3)
    if refuse:
        with pytest.raises(RuntimeError, match="controller stop refused"):
            hub.poll()
        assert pending_stops(store) == queued
    hub.poll()

    assert sender.call_count == 1 + int(refuse)
    assert sender.call_args.args[0] is bus
    sent = sent_stops(bus, agent)
    assert [(stop.key, stop.cause, stop.data["reason"]) for stop in sent] == [(agent.execution_id, event.id, reason)]
    assert pending_stops(store) == {}
    hub.poll()
    assert sender.call_count == 1 + int(refuse)
