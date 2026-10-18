from dataclasses import replace

import pytest

from hooks.context import context_recycle
from scripts.swarm import tick as tick_module
from scripts.swarm.tick import STARTUP_GRACE_MS, Placed
from tests.swarm.test_tick import FakeRuntime, tasks

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def store():
    import fakeredis

    from scripts.swarm.store import RedisStore, SwarmConfig

    result = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    result.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, compact_limit=600))
    return result


class ResumingRuntime(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.resumed = []

    def resume(self, config, agent, text):
        assert config.slug == "sw"
        assert text
        self.resumed.append(agent)
        self.live.add(agent.name)
        self.conversation_ids["reopened:pane"] = agent.conversation_id
        return Placed("reopened:pane", agent.harness, profile=agent.profile)


@pytest.mark.parametrize("lane", ["master", "eng"])
def test_dead_agent_with_room_resumes_its_conversation_and_seat(store, monkeypatch, lane):
    ledger, runtime = tasks(("t1", "eng")), ResumingRuntime()
    tick_module.tick("sw", store, ledger, runtime, 1)
    original = next(agent for agent in store.agents("sw") if agent.lane == lane)
    original = replace(original, conversation_id="original-conversation", profile="engineer")
    store.put_agent("sw", original)
    runtime.live.discard(original.name)
    monkeypatch.setattr(context_recycle, "_used", lambda session: 240_000)
    runtime.conversation_ids = {}

    tick_module.tick("sw", store, ledger, runtime, STARTUP_GRACE_MS + 2)

    assert runtime.resumed == [original]
    reopened = next(agent for agent in store.agents("sw") if agent.name == original.name)
    assert reopened.conversation_id == original.conversation_id
    assert reopened.seat == original.seat
    assert reopened.generation == original.generation
    assert reopened.task == original.task
    assert reopened.pane_id == "reopened:pane"
    if lane == "eng":
        assert ledger.rows["t1"]["claimed_by"] == original.name
        assert store.owner("sw", "t1") == original.name


def test_missing_pane_does_not_erase_a_known_conversation(store):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick_module.tick("sw", store, ledger, runtime, 1)
    original = next(agent for agent in store.agents("sw") if agent.lane == "master")
    store.put_agent("sw", replace(original, conversation_id="known-conversation"))
    runtime.conversation_ids = {}

    tick_module.tick("sw", store, ledger, runtime, 2)

    current = next(agent for agent in store.agents("sw") if agent.name == original.name)
    assert current.conversation_id == "known-conversation"
