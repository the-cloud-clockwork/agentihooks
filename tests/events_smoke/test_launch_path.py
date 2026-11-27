"""Smoke of the events launch path: a hub, hive spokes and a scratch swarm on a real nats-server and Redis."""

import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from scripts.events.config import BACKEND_ENV, BIND_ENV, SCOPED_ENV, URL_ENV
from scripts.events.envelope import Envelope
from scripts.hive import execution_stop, presence, registry
from scripts.hub import admission, claims, hive_loss, reconciler, runner
from scripts.hub import registry as hub_registry
from scripts.swarm import fleet_red, launch_check
from scripts.swarm import tick as swarm_tick
from scripts.swarm.store import EVENTS_ENGINE, MASTER, AgentRecord
from scripts.swarm.tick import IDLE_KILL_MS
from scripts.tick.adapters.github import GitHubSourceControl
from scripts.tick.steps import agents as agent_steps
from scripts.tick.steps import observe
from tests.events_smoke.conftest import LAUNCH, SLUG, Smoke
from tests.swarm.test_tick import FakeLedger, FakeRuntime

TAKE_MS = 5_000
HELD_TAKE_MS = 25_000
ANSWER_BOUND_S = admission.ANSWER_MS / 1000 / 4


def started(smoke: Smoke) -> Envelope:
    """The one execution started event the spoke published for the smoke swarm."""
    found = smoke.bus.for_swarm(SLUG).events.consumer("smoke-audit", f"swarm.{SLUG}.execution.started").fetch(1, 2_000)
    assert len(found) == 1
    return found[0].event


def worker_spawns(runtime: FakeRuntime) -> list[tuple[str, str, str]]:
    """The launches a tick pass made for any lane but the master."""
    return [spawn for spawn in runtime.spawned if spawn[0] != MASTER]


def lose_hive(smoke: Smoke, event: Envelope, emit: Any) -> None:
    """Runs a hub hive loss pass over the started execution while its hive stops beating for four presence TTLs."""
    clock = Mock(return_value=presence.now_ms())
    loss = hive_loss.HiveLoss(smoke.bus, clock, smoke.store)
    if emit is not None:
        loss._emit = emit
    loss.handle(event)
    end = clock() + 4 * presence.TTL_MS
    while clock() < end:
        clock.return_value = clock() + presence.BEAT_SECONDS * 1000 // 3
        loss.poll()


def exits(smoke: Smoke) -> list[Any]:
    """The execution exited events on the smoke swarm's bus."""
    return smoke.bus.for_swarm(SLUG).events.consumer("smoke-exits", f"swarm.{SLUG}.execution.exited").fetch(5, 1_000)


def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tick pass reads no live pull request listing."""
    monkeypatch.setattr(fleet_red, "read", lambda repo: [])


@pytest.mark.wall_clock
@pytest.mark.feature("events-smoke-pull-request-admission")
def test_a_pull_request_task_is_admitted_well_under_the_hive_wait_with_no_live_github_listing(
    smoke: Smoke, monkeypatch: pytest.MonkeyPatch
) -> None:
    listed: list[str] = []

    def live(repo: str) -> list[object]:
        listed.append(repo)
        raise AssertionError(f"admission listed {repo} on GitHub")

    monkeypatch.setattr(fleet_red, "read", live)
    monkeypatch.setattr(GitHubSourceControl, "open_changes", lambda self, **kwargs: live(self.repository))
    answered: list[float] = []
    admit = smoke.admitter.admit

    def timed(slug: str, request: dict[str, Any]) -> dict[str, Any] | None:
        began = time.monotonic()
        try:
            return admit(slug, request)
        finally:
            answered.append(time.monotonic() - began)

    monkeypatch.setattr(smoke.admitter, "admit", timed)
    hive = smoke.spoke("hive-a", "ci")
    smoke.offer("t-pr")

    assert hive.spoke.poll(TAKE_MS) == "started"

    assert listed == []
    assert smoke.gh_calls() == []
    assert len(answered) == 1 and answered[0] < ANSWER_BOUND_S
    [agent] = smoke.store.agents(SLUG)
    row = smoke.ledger.rows["t-pr"]
    assert (row["state"], row["claimed_by"]) == ("pr", agent.name)


@pytest.mark.feature("events-smoke-launch-record")
def test_the_admitted_agent_record_carries_the_harness_account_model_and_effort_the_hive_launched(
    smoke: Smoke,
) -> None:
    hive = smoke.spoke("hive-a", "eng")
    smoke.offer("t1")

    assert hive.spoke.poll(TAKE_MS) == "started"

    [agent] = smoke.store.agents(SLUG)
    assert (agent.hive, agent.task, agent.harness, agent.account, agent.model, agent.effort) == (
        "hive-a",
        "t1",
        LAUNCH["harness"],
        LAUNCH["account"],
        LAUNCH["model"],
        LAUNCH["effort"],
    )
    event = started(smoke)
    assert (event.data["name"], event.data["harness"], event.data["account"]) == (
        agent.name,
        LAUNCH["harness"],
        LAUNCH["account"],
    )


@pytest.mark.feature("events-smoke-orphan-reopen")
def test_a_claimed_task_with_no_live_agent_is_reopened_by_the_tick_on_the_events_engine(
    smoke: Smoke, monkeypatch: pytest.MonkeyPatch
) -> None:
    offline(monkeypatch)
    hive = smoke.spoke("hive-a", "eng")
    smoke.offer("t1")
    assert hive.spoke.poll(TAKE_MS) == "started"
    [agent] = smoke.store.agents(SLUG)
    assert smoke.ledger.rows["t1"]["claimed_by"] == agent.name
    smoke.store.release(SLUG, "t1", agent.name)
    smoke.store.drop_agent(SLUG, agent.name)
    smoke.store.update(SLUG, state="paused")
    assert smoke.store.config(SLUG).engine == EVENTS_ENGINE
    ledger = FakeLedger([dict(row) for row in smoke.ledger.rows.values()])

    actions = swarm_tick.tick(SLUG, smoke.store, ledger, FakeRuntime(), now_ms=presence.now_ms())

    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["claimed_by"]) == ("open", "")
    assert "task t1 had no agent, reopened" in actions


@pytest.mark.feature("events-smoke-hive-max-agents")
def test_a_hive_at_its_max_agents_takes_no_admission_and_a_hive_below_it_takes_the_offer(smoke: Smoke) -> None:
    now = presence.now_ms()
    registry.beat(smoke.store.redis, "hive-a", {"heartbeat_at": now})
    registry.update(smoke.store.redis, "hive-a", ["max-agents=1"])
    registry.beat(smoke.store.redis, "hive-b", {"heartbeat_at": now})
    registry.update(smoke.store.redis, "hive-b", ["max-agents=2"])
    full, below = smoke.spoke("hive-a", "eng"), smoke.spoke("hive-b", "eng")
    smoke.offer("t1")
    assert full.spoke.poll(TAKE_MS) == "started"
    smoke.offer("t2")

    assert full.spoke.poll(TAKE_MS) == admission.hive_admission.HELD

    assert smoke.store.claimant(SLUG, "t2") is None
    assert smoke.ledger.rows["t2"]["state"] == "open"
    assert below.spoke.poll(HELD_TAKE_MS) == "started"
    hosts = {agent.task: agent.hive for agent in smoke.store.agents(SLUG)}
    assert hosts == {"t1": "hive-a", "t2": "hive-b"}
    assert full.runtime.spawn.call_count == 1
    assert smoke.ledger.rows["t2"]["state"] == "claimed"


@pytest.mark.feature("events-smoke-no-legacy-launch")
def test_no_legacy_launch_path_runs_while_the_spoke_launches_the_work(
    smoke: Smoke, monkeypatch: pytest.MonkeyPatch
) -> None:
    hive = smoke.spoke("hive-a", "eng")
    smoke.offer("t1")
    assert hive.spoke.poll(TAKE_MS) == "started"
    [agent] = smoke.store.agents(SLUG)
    ledger = FakeLedger([dict(row) for row in smoke.ledger.rows.values()])
    runtime = FakeRuntime()
    offline(monkeypatch)

    swarm_tick.tick(SLUG, smoke.store, ledger, runtime, now_ms=presence.now_ms())

    assert worker_spawns(runtime) == []
    assert ledger.rows["t2"]["state"] == "open"
    assert hive.runtime.spawn.call_count == 1
    assert hive.runtime.spawn.call_args.args[0].name == agent.name


@pytest.mark.feature("events-smoke-execution-stop")
def test_the_hub_stops_a_seated_execution_and_its_hive_reaps_it_and_publishes_the_exit(smoke: Smoke) -> None:
    hive = smoke.spoke("hive-a", "eng")
    smoke.offer("t1")
    assert hive.spoke.poll(TAKE_MS) == "started"
    [agent] = smoke.store.agents(SLUG)
    event = started(smoke)
    stopper = execution_stop.Stopper(hive.spoke.bus, SLUG, "hive-a", hive.runtime)

    assert execution_stop.stop(smoke.bus, SLUG, agent, "launch check failed") == execution_stop.SENT
    assert stopper.poll(TAKE_MS) == [execution_stop.STOPPED]

    hive.runtime.reap.assert_called_once_with(agent.name)
    [exited] = exits(smoke)
    assert (exited.event.id, exited.event.cause) == (f"exited:{event.id}", event.id)
    assert exited.event.data["claim_key"] == event.data["claim_key"]
    assert execution_stop.stop(smoke.bus, SLUG, agent, "again") == execution_stop.ENDED
    assert stopper.poll(1_000) == []


@dataclass
class Seated:
    """An agent the hub admitted for t1 on hive-a, the hive spoke running it, its start and the tick's runtime."""

    hive: SimpleNamespace
    agent: AgentRecord
    event: Envelope
    runtime: FakeRuntime


def seated(smoke: Smoke, monkeypatch: pytest.MonkeyPatch) -> Seated:
    """Admits t1 through the hub for a hive-a spoke, runs the hub claim lives reactor over the start the spoke
    published, clears the pending launch check as the hub's launch checks do once one passes, and points the tick's
    bus at the smoke's nats-server with the hub's scoped provisioning settings; the tick's runtime sees the agent's
    pane live."""
    monkeypatch.delenv(BIND_ENV, raising=False)
    for name, value in ((BACKEND_ENV, "nats"), (URL_ENV, smoke.url), (SCOPED_ENV, "1")):
        monkeypatch.setenv(name, value)
    offline(monkeypatch)
    hive = smoke.spoke("hive-a", "eng")
    smoke.offer("t1")
    assert hive.spoke.poll(TAKE_MS) == "started"
    hub = runner.HubRunner(smoke.bus, hub_registry.Registry([claims.Claims(smoke.store).reactor()]), "hub")
    hub.run_once(timeout_ms=TAKE_MS)
    hub.stop()
    [agent] = smoke.store.agents(SLUG)
    launch_check.forget(smoke.store, SLUG, agent.name)
    assert smoke.store.claims(SLUG, "t1") == 1
    assert reconciler.legacy_agents(smoke.store, SLUG) == []
    runtime = FakeRuntime()
    runtime.live.add(agent.name)
    return Seated(hive, agent, started(smoke), runtime)


def ledger_rows(smoke: Smoke) -> dict[str, dict[str, Any]]:
    """The rows of the ledger the hub claimed through, by task id, as a tick pass reads them."""
    return {row["id"]: row for row in smoke.ledger.tasks(SLUG)}


def assert_retired(smoke: Smoke, seat: Seated, reason: str) -> None:
    """Asserts the tick retired the seated agent for ``reason``: its runtime reaped it, the store dropped it and gave
    back its claim life, the task reopened on the ledger the hub claimed it through, and the hub's stop reached its
    hive, which reaped it and published its exit naming ``reason``."""
    stopper = execution_stop.Stopper(seat.hive.spoke.bus, SLUG, "hive-a", seat.hive.runtime)

    assert stopper.poll(TAKE_MS) == [execution_stop.STOPPED]

    seat.hive.runtime.reap.assert_called_once_with(seat.agent.name)
    [exited] = exits(smoke)
    assert (exited.event.cause, exited.event.data["stop_reason"]) == (seat.event.id, reason)
    assert seat.runtime.killed == [seat.agent.name]
    assert smoke.store.agents(SLUG) == []
    assert smoke.store.claimant(SLUG, "t1") is None
    assert smoke.store.claims(SLUG, "t1") == 0
    assert (smoke.ledger.rows["t1"]["state"], smoke.ledger.rows["t1"]["claimed_by"]) == ("open", "")


@pytest.mark.feature("events-smoke-mismatch-retire")
def test_a_hub_admitted_agent_bound_to_another_account_is_retired_stopped_on_its_hive_and_refunded(
    smoke: Smoke, monkeypatch: pytest.MonkeyPatch
) -> None:
    seat = seated(smoke, monkeypatch)
    bound = seat.runtime.bindings
    monkeypatch.setattr(
        seat.runtime,
        "bindings",
        lambda agents: {name: {**facts, "account": "home"} for name, facts in bound(agents).items()},
    )

    actions = observe._verify(SLUG, smoke.store, smoke.ledger, seat.runtime, ledger_rows(smoke), presence.now_ms())

    assert actions == [f"retired {seat.agent.name} after mismatched account, task t1 reopened"]
    assert_retired(smoke, seat, "mismatch")


@pytest.mark.feature("events-smoke-idle-retire")
def test_a_hub_admitted_agent_idle_for_the_kill_time_is_retired_stopped_on_its_hive_and_refunded(
    smoke: Smoke, monkeypatch: pytest.MonkeyPatch
) -> None:
    seat = seated(smoke, monkeypatch)
    seat.runtime.statuses[seat.agent.name] = "idle"
    began = presence.now_ms()
    first = agent_steps._reap(SLUG, smoke.store, smoke.ledger, seat.runtime, ledger_rows(smoke), began)
    assert first == []
    assert [agent.name for agent in smoke.store.agents(SLUG)] == [seat.agent.name]
    assert smoke.store.claims(SLUG, "t1") == 1

    actions = agent_steps._reap(SLUG, smoke.store, smoke.ledger, seat.runtime, ledger_rows(smoke), began + IDLE_KILL_MS)

    assert actions == [f"stalled {seat.agent.name}, task t1 reopened"]
    assert_retired(smoke, seat, "idle")
