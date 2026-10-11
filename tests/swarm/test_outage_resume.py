from dataclasses import replace

import pytest

from scripts.swarm import outage
from scripts.swarm.pane import PaneObservation
from scripts.swarm.tick import Placed, tick
from tests.swarm.test_tick import FakeRuntime, tasks

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
    assert all(runtime.statuses[agent.name] == "working" for agent in agents)
    assert {agent.task: store.claimant("sw", agent.task) for agent in agents} == claims
    assert runtime.spawned == launches
    assert runtime.nudged == [agents[0].name, agents[1].name]
    assert runtime.resumed[0][:2] == (agents[2].name, "conversation-2")
    for _, text in runtime.nudge_calls:
        assert "Service is back" in text
        assert "steering.md" in text and "progress.md" in text and "proof.md" in text
        assert "agentihooks msg inbox" in text
        assert "continue your own task" in text
