import json
from collections.abc import Iterator
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.events.memory import open_bus
from scripts.hive import execution_events, execution_stop, join_report
from scripts.hub.launch_checks import HUNG_MS, STOPPING, LaunchChecks
from scripts.swarm import commands, launch_check
from scripts.swarm.store import AgentRecord
from tests.hub.test_admission import LAUNCH, SLUG, ask
from tests.hub.test_admission import rig as rig
from tests.hub.test_claim_lives import started

SKEWED = 1


@pytest.fixture(autouse=True)
def root_hive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(commands, "hive_id", lambda: "root")


@pytest.fixture
def bus() -> Iterator[Any]:
    opened: Any = open_bus(BusConfig())
    yield opened
    opened.close()


def checker(rig: SimpleNamespace, bus: Any, stop: Any = execution_stop.stop) -> LaunchChecks:
    return LaunchChecks(bus, rig.store, rig.admitter, clock=lambda: 0, stop=stop)


def spawned(bus: Any, admitted: dict[str, Any]) -> Envelope:
    event = started(admitted)
    event = replace(event, time=SKEWED, data={**event.data, "claim_revision": 1, "launched_at": SKEWED})
    value = {"state": "started", "claim_revision": 1, "event": event.to_dict()}
    bus.for_swarm(SLUG).state("spoke-work").create("claim", json.dumps(value).encode())
    execution_events.started(bus, event)
    return event


def joined(rig: SimpleNamespace) -> None:
    [agent] = rig.store.agents(SLUG)
    checks = dict.fromkeys((*join_report.REQUIRED, *join_report.OPTIONAL), True)
    join_report.start(rig.store.redis, SLUG, agent, "session", checks, {}, SKEWED)


def stops(bus: Any) -> list[Envelope]:
    subject = execution_stop.stop_subject(SLUG, "hive-a")
    durable = execution_stop.durable(SLUG, "hive-a")
    found = bus.for_swarm(SLUG).stops.consumer(durable, subject, execution_stop.POLICY).fetch(10, 0)
    return [delivery.event for delivery in found]


@pytest.mark.feature("events-launch-check-timing")
def test_admission_starts_a_hub_owned_launch_check(rig: SimpleNamespace) -> None:
    admitted = rig.admitter.admit(SLUG, ask())

    assert launch_check.pending(rig.store, SLUG) == {
        admitted["name"]: {"task": "t1", "at": 7_000, "relaunch": True, "owner": "hub"}
    }


@pytest.mark.feature("events-launch-check-timing")
def test_a_skewed_slow_spawn_is_timed_on_the_hub_clock_from_its_started_record(rig: SimpleNamespace, bus: Any) -> None:
    admitted = rig.admitter.admit(SLUG, ask())
    spawned(bus, admitted)
    checks = checker(rig, bus)

    assert checks.check(SLUG, 800_000) == []
    assert checks.check(SLUG, 859_999) == []
    assert stops(bus) == []

    [action] = checks.check(SLUG, 860_000)

    assert action.startswith(f"stop sent for {admitted['name']} because its launch check failed on hive_join")
    [stop] = stops(bus)
    assert (stop.type, stop.data["name"], stop.data["reason"]) == (
        execution_stop.TYPE,
        admitted["name"],
        "its launch check failed on hive_join",
    )
    report = launch_check.report(rig.store, SLUG, "t1")
    assert (report["state"], report["elapsed_ms"]) == ("failed", 60_000)
    assert launch_check.pending(rig.store, SLUG)[admitted["name"]][STOPPING] == 860_000


@pytest.mark.feature("events-launch-check-timing")
def test_a_started_agent_that_joined_passes_although_its_launch_time_is_unset(rig: SimpleNamespace, bus: Any) -> None:
    admitted = rig.admitter.admit(SLUG, ask())
    spawned(bus, admitted)
    joined(rig)
    checks = checker(rig, bus)

    assert checks.check(SLUG, 7_000 + HUNG_MS) == [f"{admitted['name']} passed its launch check in 0 seconds"]

    [agent] = rig.store.agents(SLUG)
    assert agent.launched_at == 0
    assert launch_check.pending(rig.store, SLUG) == {}
    assert stops(bus) == []


@pytest.mark.feature("events-launch-check-hung-spawn")
def test_a_hung_spawn_is_freed_through_the_real_stop_and_its_task_reopened(rig: SimpleNamespace, bus: Any) -> None:
    admitted = rig.admitter.admit(SLUG, ask())
    execution_events.prepare(bus, SLUG, admitted["name"], admitted["task"]["execution_generation"])
    checks = checker(rig, bus)

    assert checks.check(SLUG, 10_000) == []
    assert checks.check(SLUG, 10_000 + HUNG_MS - 1) == []

    [action] = checks.check(SLUG, 10_000 + HUNG_MS)

    assert action == (
        f"freed {admitted['name']} and reopened t1 because its launch check failed on launched and no hive started it"
    )
    assert rig.store.agents(SLUG) == []
    assert rig.store.claimant(SLUG, "t1") is None
    assert rig.rows["t1"]["state"] == "open"
    assert launch_check.pending(rig.store, SLUG) == {}
    assert launch_check.report(rig.store, SLUG, "t1")["misses"] == {
        "launched": {
            "expected": f"a started execution within {HUNG_MS // 1000} seconds of the hive taking it",
            "actual": "none started",
        }
    }


@pytest.mark.feature("events-launch-check-hung-spawn")
def test_a_spawn_the_hive_never_began_is_judged_from_admission(rig: SimpleNamespace, bus: Any) -> None:
    rig.admitter.admit(SLUG, ask())
    checks = checker(rig, bus)

    assert checks.check(SLUG, 7_000 + HUNG_MS - 1) == []
    [action] = checks.check(SLUG, 7_000 + HUNG_MS)

    assert action.startswith("freed ")
    assert rig.rows["t1"]["state"] == "open"


@pytest.mark.feature("events-launch-check-hung-spawn")
def test_a_late_launch_report_after_the_verdict_revives_nothing(rig: SimpleNamespace, bus: Any) -> None:
    rig.admitter.admit(SLUG, ask())
    checker(rig, bus).check(SLUG, 7_000 + HUNG_MS)

    assert rig.admitter.launched(SLUG, ask(launch=LAUNCH)) is False

    assert rig.store.agents(SLUG) == []
    assert launch_check.pending(rig.store, SLUG) == {}
    assert rig.rows["t1"]["state"] == "open"


@pytest.mark.feature("events-launch-check-hung-spawn")
def test_a_redelivered_offer_after_a_free_never_answers_the_dropped_admission(rig: SimpleNamespace, bus: Any) -> None:
    stale = rig.admitter.admit(SLUG, ask())
    checker(rig, bus).check(SLUG, 7_000 + HUNG_MS)

    again = rig.admitter.admit(SLUG, ask())

    assert again is None or again.get("name") != stale["name"]
    assert again is None or again["name"] in rig.store.listed(SLUG)
    assert stale["name"] not in rig.store.listed(SLUG)


@pytest.mark.feature("events-launch-check-hung-spawn")
def test_a_raising_stop_keeps_the_check_and_sends_again_one_deadline_later(rig: SimpleNamespace, bus: Any) -> None:
    admitted = rig.admitter.admit(SLUG, ask())
    spawned(bus, admitted)
    calls: list[int] = []

    def refused(bus: Any, slug: str, agent: AgentRecord, reason: str) -> str:
        calls.append(len(calls))
        if len(calls) == 1:
            raise ConnectionError("bus gone")
        return execution_stop.stop(bus, slug, agent, reason)

    checks = checker(rig, bus, refused)
    checks.check(SLUG, 0)
    with pytest.raises(ConnectionError):
        checks.check(SLUG, 60_000)

    assert launch_check.pending(rig.store, SLUG)[admitted["name"]][STOPPING] == 60_000
    assert checks.check(SLUG, 119_999) == []
    [action] = checks.check(SLUG, 120_000)

    assert action.startswith(f"stop sent for {admitted['name']}")
    assert len(stops(bus)) == 1
    assert launch_check.report(rig.store, SLUG, "t1")["at"] == 60_000


@pytest.mark.feature("events-launch-check-hung-spawn")
def test_a_sent_stop_holds_the_check_until_the_exit_drops_the_agent(rig: SimpleNamespace, bus: Any) -> None:
    admitted = rig.admitter.admit(SLUG, ask())
    spawned(bus, admitted)
    checks = checker(rig, bus)
    checks.check(SLUG, 0)
    checks.check(SLUG, 60_000)

    assert checks.check(SLUG, 90_000) == []
    assert admitted["name"] in launch_check.pending(rig.store, SLUG)

    rig.store.drop_agent(SLUG, admitted["name"])

    assert checks.check(SLUG, 90_000) == []
    assert launch_check.pending(rig.store, SLUG) == {}


@pytest.mark.feature("events-launch-check-tick-filter")
def test_the_tick_leaves_a_hub_owned_launch_check_to_the_hub(rig: SimpleNamespace) -> None:
    from scripts.tick.steps import observe

    admitted = rig.admitter.admit(SLUG, ask())
    entry = {"task": "t1", "at": 7_000, "relaunch": True, "owner": "hub"}
    rig.store.redis.hset(rig.store.key(SLUG, "launch-checks"), admitted["name"], json.dumps(entry))

    def retire(agent: AgentRecord, homes: tuple[Any, ...] = ()) -> bool:
        raise AssertionError(f"the tick retired hub owned {agent.name}")

    runtime = SimpleNamespace(bindings=lambda agents: {}, retire=retire, live_names=lambda: set[str]())

    assert observe._launch_checks(SLUG, rig.store, rig.ledger, runtime, rig.rows, {}, 10**9) == []

    assert launch_check.pending(rig.store, SLUG) == {admitted["name"]: entry}
    assert rig.store.agents(SLUG)[0].name == admitted["name"]


@pytest.mark.feature("events-launch-placement")
def test_a_launch_report_writes_the_overlays_and_profile_decision_the_hive_placed(rig: SimpleNamespace) -> None:
    rig.admitter.admit(SLUG, ask())
    decision = {"validation": {"state": "pending"}, launch_check.HOST_OVERLAYS: ["engineer", "frontend"]}
    launch = {**LAUNCH, "overlays": ["frontend"], "profile_decision": decision}

    assert rig.admitter.launched(SLUG, ask(launch=launch)) is True

    [agent] = rig.store.agents(SLUG)
    assert (agent.overlays, agent.profile_decision) == (["frontend"], decision)
    assert launch_check.expected(agent) == ["engineer", "frontend"]


@pytest.mark.feature("events-launch-check-hung-spawn")
def test_the_hub_registry_polls_launch_checks_on_the_real_stop(rig: SimpleNamespace, bus: Any) -> None:
    from scripts.hub import registry

    rig.admitter.admit(SLUG, ask())
    found = {reactor.name: reactor for reactor in registry.configured(bus, lambda: 10**9)}
    reactor = found["launch-checks"]
    assert (reactor.subject, reactor.singleton) == ("swarm.*.launch-checks.requested", True)
    poll: Any = reactor.poll
    poll.__self__._store, poll.__self__._admission = rig.store, rig.admitter

    poll(lambda: True)

    assert rig.store.agents(SLUG) == []
    assert rig.rows["t1"]["state"] == "open"
