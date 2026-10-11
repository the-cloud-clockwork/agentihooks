from dataclasses import replace

import pytest

from scripts.swarm import outage
from scripts.swarm.pane import PaneObservation
from scripts.swarm.tick import Placed, SpawnError, tick
from tests.swarm.test_tick import FakeRuntime, tasks
from tests.swarm.test_tick import store as store

pytestmark = pytest.mark.unit

MINUTE = 60_000


class RecoveringRuntime(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.errors, self.resumed = {}, []

    def observe(self, agent):
        return PaneObservation(self.status(agent), provider_error=self.errors.get(agent.name, ""))

    def nudge(self, agent, text):
        result = super().nudge(agent, text)
        if not result:
            self.statuses[agent.name] = "working"
            self.errors.pop(agent.name, None)
        return result

    def resume(self, config, agent, text):
        self.resumed.append((agent.name, agent.conversation_id, text))
        self.live.add(agent.name)
        self.statuses[agent.name] = "working"
        return Placed("recovered:pane", agent.harness, profile=agent.profile)


@pytest.fixture(autouse=True)
def outage_home(tmp_path, monkeypatch):
    monkeypatch.setattr(outage, "HOME", tmp_path)


def three_owners(store):
    store.update("sw", max_eng=3)
    ledger = tasks(("t1", "eng"), ("t2", "eng"), ("t3", "eng"))
    runtime = RecoveringRuntime()
    tick("sw", store, ledger, runtime, 1_000)
    store.update("sw", state="paused")
    agents = [agent for agent in store.agents("sw") if agent.lane == "eng"]
    for index, agent in enumerate(agents):
        store.put_agent("sw", replace(agent, conversation_id=f"conversation-{index}", profile="engineer"))
        runtime.statuses[agent.name] = "idle"
    return ledger, runtime, agents


def test_error_idle_and_dead_owners_resume_their_tasks_within_three_recovery_ticks(store, tmp_path, monkeypatch):
    from scripts.swarm import snapshot

    ledger, runtime, agents = three_owners(store)
    monkeypatch.setattr(snapshot, "worktrees", lambda repo, names: {name: str(tmp_path) for name in names})
    claims = {agent.task: store.claimant("sw", agent.task) for agent in agents}
    launches = list(runtime.spawned)
    outage.observe("sw", "provider", ["provider unavailable"], MINUTE)
    runtime.errors[agents[0].name] = "provider unavailable"
    runtime.live.remove(agents[2].name)
    tick("sw", store, ledger, runtime, 8 * MINUTE)
    assert {agent.task: store.claimant("sw", agent.task) for agent in agents} == claims
    outage.observe("sw", "provider", [], 9 * MINUTE)
    for step in range(3):
        tick("sw", store, ledger, runtime, (9 + step) * MINUTE)
    assert all(runtime.statuses[agent.name] == "working" for agent in agents), (
        runtime.statuses,
        runtime.resumed,
        runtime.spawned,
    )
    assert {agent.task: store.claimant("sw", agent.task) for agent in agents} == claims
    assert runtime.spawned == launches
    assert runtime.nudged == [agents[0].name, agents[1].name]
    assert runtime.resumed[0][:2] == (agents[2].name, "conversation-2")
    for _, text in runtime.nudge_calls:
        assert "Service is back" in text
        assert "steering.md" in text and "progress.md" in text and "proof.md" in text
        assert "agentihooks msg inbox" in text
        assert "continue your own task" in text


def test_a_successor_claim_during_restart_prevents_resuming_the_old_owner(store, tmp_path, monkeypatch):
    from scripts.swarm import snapshot

    ledger, runtime, agents = three_owners(store)
    old = agents[2]
    outage.observe("sw", "provider", ["provider unavailable"], MINUTE)
    runtime.live.remove(old.name)
    tick("sw", store, ledger, runtime, 8 * MINUTE)
    outage.observe("sw", "provider", [], 9 * MINUTE)

    def takeover(repo, names):
        assert store.release("sw", old.task, old.name)
        assert store.claim("sw", old.task, "successor", 600_000)
        ledger.rows[old.task]["claimed_by"] = "successor"
        return {old.name: str(tmp_path)}

    monkeypatch.setattr(snapshot, "worktrees", takeover)
    launches = list(runtime.spawned)
    tick("sw", store, ledger, runtime, 9 * MINUTE)
    assert not runtime.resumed
    assert runtime.spawned == launches
    assert store.claimant("sw", old.task) == "successor"
    assert ledger.rows[old.task]["claimed_by"] == "successor"


@pytest.mark.parametrize("failure", ["unsupported", "ambiguous"])
def test_a_definite_resume_blocker_uses_the_handoff_but_an_ambiguous_launch_does_not_duplicate_it(
    store, tmp_path, monkeypatch, failure
):
    from scripts.swarm import snapshot

    ledger, runtime, agents = three_owners(store)
    old = agents[2]
    monkeypatch.setattr(snapshot, "worktrees", lambda repo, names: {name: str(tmp_path) for name in names})
    handoff = "## Next\nContinue the current task from its recorded proof.\n"
    store.put_handoff("sw", old.task, handoff, seat=old.seat)
    outage.observe("sw", "provider", ["provider unavailable"], MINUTE)
    runtime.live.remove(old.name)
    tick("sw", store, ledger, runtime, 8 * MINUTE)
    outage.observe("sw", "provider", [], 9 * MINUTE)

    def blocked(config, agent, text):
        raise SpawnError("conversation resume blocked", failure)

    monkeypatch.setattr(runtime, "resume", blocked)
    launches = len(runtime.spawned)
    tick("sw", store, ledger, runtime, 9 * MINUTE)
    if failure == "unsupported":
        assert len(runtime.spawned) == launches + 1
        assert runtime.spawned[-1] == ("eng", old.name, old.task)
        assert runtime.tasks[-1]["handoff"] == handoff
        assert next(agent for agent in store.agents("sw") if agent.name == old.name).seat == old.seat
    else:
        assert len(runtime.spawned) == launches
    assert store.claimant("sw", old.task) == old.name
    assert ledger.rows[old.task]["claimed_by"] == old.name


def test_work_after_outage_closure_excludes_an_agent_that_only_becomes_idle_later(store):
    from scripts.swarm import idle

    ledger, runtime, agents = three_owners(store)
    healthy = agents[0]
    runtime.statuses[healthy.name] = "working"
    outage.observe("sw", "provider", ["provider unavailable"], MINUTE)
    tick("sw", store, ledger, runtime, 8 * MINUTE)
    outage.observe("sw", "provider", [], 9 * MINUTE)
    tick("sw", store, ledger, runtime, 9 * MINUTE)
    runtime.statuses[healthy.name] = "idle"
    idle.beat(store.redis, "sw", healthy.name, "idle", 10 * MINUTE)
    tick("sw", store, ledger, runtime, 10 * MINUTE)
    assert healthy.name not in runtime.nudged
    assert next(agent for agent in store.agents("sw") if agent.name == healthy.name).idle_ticks == 1


def test_overlapping_outages_hold_recovery_and_persistent_errors_retry_three_times_then_count_idle(store):
    from scripts.swarm import idle
    from scripts.swarm.store import RedisStore

    ledger, runtime, agents = three_owners(store)
    stuck = agents[0]
    runtime.errors[stuck.name] = "provider unavailable"
    runtime.nudge_refusals[stuck.name] = "provider unavailable"
    outage.observe("sw", "provider", ["provider unavailable"], MINUTE)
    outage.observe("sw", "redis", ["Redis unavailable"], 8 * MINUTE)
    tick("sw", store, ledger, runtime, 8 * MINUTE)
    outage.observe("sw", "provider", [], 9 * MINUTE)
    tick("sw", store, ledger, runtime, 9 * MINUTE)
    assert not runtime.nudged
    outage.observe("sw", "redis", [], 10 * MINUTE)
    for at in (10, 11, 12):
        store = RedisStore(store.redis)
        tick("sw", store, ledger, runtime, at * MINUTE)
        idle.beat(store.redis, "sw", stuck.name, "idle", at * MINUTE + 1)
    assert runtime.nudged.count(stuck.name) == 3
    tick("sw", store, ledger, runtime, 13 * MINUTE)
    assert runtime.nudged.count(stuck.name) == 3
    assert next(agent for agent in store.agents("sw") if agent.name == stuck.name).idle_ticks == 1
    assert store.claimant("sw", stuck.task) == stuck.name
