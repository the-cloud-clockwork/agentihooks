"""Smoke of the events launch path: a hub, hive spokes and a scratch swarm on a real nats-server and Redis."""

import json
import time
from typing import Any
from unittest.mock import Mock

import pytest

from scripts.events.envelope import Envelope
from scripts.hive import presence, registry
from scripts.hub import admission, hive_loss
from scripts.swarm import engine_migration, fleet_red, readiness
from scripts.swarm import tick as swarm_tick
from scripts.swarm.execution import TERMINAL_STATES
from scripts.swarm.store import EVENTS_ENGINE, MASTER, AgentRecord, SwarmConfig
from scripts.tick.adapters.github import GitHubSourceControl
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


@pytest.mark.feature("events-smoke-engine-switch")
def test_an_engine_switch_exits_no_running_execution(smoke: Smoke) -> None:
    hive = smoke.spoke("hive-a", "eng")
    smoke.offer("t1")
    assert hive.spoke.poll(TAKE_MS) == "started"
    event = started(smoke)
    smoke.store.create(SwarmConfig("legacy-busy", smoke.store.config(SLUG).repo, 1, 1))
    record = AgentRecord(
        smoke.store.next_name("legacy-busy", "eng"), "eng", "t9", seat="eng-1@legacy-busy", started_at=1
    )
    smoke.store.start_execution("legacy-busy", record)
    before = {slug: smoke.store.agents(slug) for slug in (SLUG, "legacy-busy")}

    outcomes = engine_migration.migrate(smoke.store)

    assert outcomes == {SLUG: "unchanged", "legacy-busy": engine_migration.BUSY}
    assert smoke.store.config("legacy-busy").engine == "legacy"
    smoke.store.update(SLUG, engine="legacy")
    lose_hive(smoke, event, None)

    assert exits(smoke) == []
    claim = smoke.bus.for_swarm(SLUG).state("spoke-work").get(event.data["claim_key"])
    assert claim is not None
    assert json.loads(claim.value)["state"] == "started"
    for slug, agents in before.items():
        assert smoke.store.agents(slug) == agents
        assert all(agent.state not in TERMINAL_STATES for agent in agents)
    smoke.store.update(SLUG, engine=EVENTS_ENGINE)
    lose_hive(smoke, event, None)
    assert [found.event.data["reason"] for found in exits(smoke)] == [hive_loss.REASON]


@pytest.mark.feature("events-smoke-no-legacy-launch")
def test_no_legacy_launch_path_runs_while_the_spoke_launches_the_work(
    smoke: Smoke, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy: list[tuple[Any, ...]] = []

    def launched(*args: Any) -> list[str]:
        legacy.append(args)
        return []

    monkeypatch.setattr(swarm_tick, "_spawn", launched)
    hive = smoke.spoke("hive-a", "eng")
    smoke.offer("t1")
    assert hive.spoke.poll(TAKE_MS) == "started"
    [agent] = smoke.store.agents(SLUG)
    ledger = FakeLedger([dict(row) for row in smoke.ledger.rows.values()])
    runtime = FakeRuntime()
    offline(monkeypatch)
    smoke.store.redis.set(smoke.store.key(SLUG, swarm_tick.SPAWN_DUE), 1)

    swarm_tick.tick(SLUG, smoke.store, ledger, runtime, now_ms=presence.now_ms())

    assert not readiness.tick_launches(smoke.store.config(SLUG).engine)
    assert legacy == []
    assert worker_spawns(runtime) == []
    assert ledger.rows["t2"]["state"] == "open"
    assert hive.runtime.spawn.call_count == 1
    assert hive.runtime.spawn.call_args.args[0].name == agent.name
