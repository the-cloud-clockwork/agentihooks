import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.hive import join_report
from scripts.hub import claims
from scripts.swarm import commands, launch_check
from scripts.swarm.store import AgentRecord
from tests.hub.test_admission import LAUNCH, SLUG, ask
from tests.hub.test_admission import rig as rig
from tests.hub.test_claim_lives import started


@pytest.fixture(autouse=True)
def root_hive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(commands, "hive_id", lambda: "root")


def checker(rig: SimpleNamespace) -> tuple[Any, list[tuple[str, str, str]]]:
    from scripts.hub.launch_checks import LaunchChecks

    stops: list[tuple[str, str, str]] = []

    def stop(slug: str, agent: AgentRecord, reason: str) -> None:
        stops.append((slug, agent.name, reason))

    return LaunchChecks(stop, rig.store, clock=lambda: 0), stops


def start(rig: SimpleNamespace, launched_at: int) -> dict:
    admitted = rig.admitter.admit(SLUG, ask())
    event = started(admitted)
    claims.Claims(rig.store).handle(replace(event, data={**event.data, "launched_at": launched_at}))
    return admitted


def joined(rig: SimpleNamespace, at: int) -> None:
    [agent] = rig.store.agents(SLUG)
    checks = dict.fromkeys((*join_report.REQUIRED, *join_report.OPTIONAL), True)
    join_report.start(rig.store.redis, SLUG, agent, "session", checks, {}, at)


@pytest.mark.feature("events-launch-check-timing")
def test_admission_starts_a_hub_owned_launch_check(rig: SimpleNamespace) -> None:
    admitted = rig.admitter.admit(SLUG, ask())

    assert launch_check.pending(rig.store, SLUG) == {
        admitted["name"]: {"task": "t1", "at": 7_000, "relaunch": True, "owner": "hub"}
    }


@pytest.mark.feature("events-launch-check-timing")
def test_a_slow_spawn_is_timed_from_its_reported_launch_not_its_admission(rig: SimpleNamespace) -> None:
    admitted = start(rig, 200_000)
    checks, stops = checker(rig)

    assert checks.check(SLUG, 259_999) == []
    assert stops == []
    assert admitted["name"] in launch_check.pending(rig.store, SLUG)

    [action] = checks.check(SLUG, 260_000)

    assert stops == [(SLUG, admitted["name"], "its launch check failed on hive_join")]
    assert action.startswith(f"stopped {admitted['name']} after its launch check failed on hive_join")
    assert launch_check.pending(rig.store, SLUG) == {}
    report = launch_check.report(rig.store, SLUG, "t1")
    assert (report["state"], report["elapsed_ms"]) == ("failed", 60_000)


@pytest.mark.feature("events-launch-check-timing")
def test_a_launched_agent_that_joined_passes_without_a_stop(rig: SimpleNamespace) -> None:
    admitted = start(rig, 200_000)
    joined(rig, 210_000)
    checks, stops = checker(rig)

    assert checks.check(SLUG, 215_000) == [f"{admitted['name']} passed its launch check in 15 seconds"]

    assert stops == []
    assert launch_check.pending(rig.store, SLUG) == {}
    assert launch_check.report(rig.store, SLUG, "t1")["state"] == "passed"


@pytest.mark.feature("events-launch-check-hung-spawn")
def test_a_spawn_that_reports_no_launch_is_stopped_one_launch_window_after_admission(rig: SimpleNamespace) -> None:
    from scripts.hub.launch_checks import LAUNCH_WINDOW_MS

    admitted = rig.admitter.admit(SLUG, ask())
    checks, stops = checker(rig)

    assert checks.check(SLUG, 7_000 + LAUNCH_WINDOW_MS - 1) == []
    assert stops == []

    checks.check(SLUG, 7_000 + LAUNCH_WINDOW_MS)

    assert stops == [(SLUG, admitted["name"], "its launch check failed on launched")]
    report = launch_check.report(rig.store, SLUG, "t1")
    assert report["misses"] == {
        "launched": {"expected": "a launch reported within 300 seconds of admission", "actual": "none reported"}
    }
    assert report["elapsed_ms"] == LAUNCH_WINDOW_MS


@pytest.mark.feature("events-launch-check-hung-spawn")
def test_a_failed_check_relaunches_its_task_once_then_reports_the_relaunch_spent(rig: SimpleNamespace) -> None:
    admitted = start(rig, 200_000)
    checks, _ = checker(rig)

    [first] = checks.check(SLUG, 260_000)
    [agent] = rig.store.agents(SLUG)
    launch_check.begin(rig.store, SLUG, agent, 260_000, owner=launch_check.HUB)
    [second] = checks.check(SLUG, 320_000)

    assert first.endswith("its task is relaunched once")
    assert second.endswith("its one relaunch is spent")
    assert launch_check.relaunched(rig.store, SLUG, "t1")
    assert admitted["name"] == agent.name


@pytest.mark.feature("events-launch-check-hung-spawn")
def test_a_check_for_an_agent_no_longer_listed_is_forgotten(rig: SimpleNamespace) -> None:
    admitted = rig.admitter.admit(SLUG, ask())
    rig.store.drop_agent(SLUG, admitted["name"])
    checks, stops = checker(rig)

    assert checks.check(SLUG, 10**9) == []

    assert stops == []
    assert launch_check.pending(rig.store, SLUG) == {}


@pytest.mark.feature("events-launch-check-tick-filter")
def test_the_tick_leaves_a_hub_owned_launch_check_to_the_hub(rig: SimpleNamespace) -> None:
    from scripts.tick.steps import observe

    admitted = rig.admitter.admit(SLUG, ask())
    entry = {"task": "t1", "at": 7_000, "relaunch": True, "owner": "hub"}
    rig.store.redis.hset(rig.store.key(SLUG, "launch-checks"), admitted["name"], json.dumps(entry))

    def retire(agent: AgentRecord, homes: tuple = ()) -> bool:
        raise AssertionError(f"the tick retired hub owned {agent.name}")

    runtime = SimpleNamespace(bindings=lambda agents: {}, retire=retire, live_names=lambda: set())

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
def test_the_hub_registry_polls_launch_checks_that_stop_through_the_execution_stop(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.events.config import BusConfig
    from scripts.events.memory import open_bus
    from scripts.hive import execution_stop
    from scripts.hub import registry

    sent: list[tuple[Any, str, str, str]] = []

    def stop(bus: Any, slug: str, agent: AgentRecord, reason: str) -> str:
        sent.append((bus, slug, agent.name, reason))
        return execution_stop.SENT

    monkeypatch.setattr(execution_stop, "stop", stop)
    admitted = rig.admitter.admit(SLUG, ask())
    bus: Any = open_bus(BusConfig())
    try:
        found = {reactor.name: reactor for reactor in registry.configured(bus, lambda: 10**9)}
        reactor = found["launch-checks"]
        assert (reactor.subject, reactor.singleton) == ("swarm.*.launch-checks.requested", True)
        poll: Any = reactor.poll
        poll.__self__._store = rig.store
        poll(lambda: True)
    finally:
        bus.close()

    assert sent == [(bus, SLUG, admitted["name"], "its launch check failed on launched")]
