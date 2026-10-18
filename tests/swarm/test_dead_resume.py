from dataclasses import replace
from types import SimpleNamespace

import pytest

from hooks.context import context_recycle
from scripts.swarm import tick as tick_module
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.tick import STARTUP_GRACE_MS, Placed
from tests.swarm.profile_fixture import validated
from tests.swarm.test_runtime import _resuming
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
        assert store.claimant("sw", "t1") == original.name


def test_missing_pane_does_not_erase_a_known_conversation(store):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick_module.tick("sw", store, ledger, runtime, 1)
    original = next(agent for agent in store.agents("sw") if agent.lane == "master")
    store.put_agent("sw", replace(original, conversation_id="known-conversation"))
    runtime.conversation_ids = {}

    tick_module.tick("sw", store, ledger, runtime, 2)

    current = next(agent for agent in store.agents("sw") if agent.name == original.name)
    assert current.conversation_id == "known-conversation"


@pytest.mark.parametrize("lane", ["master", "eng"])
def test_launch_persists_the_native_conversation(tmp_path, monkeypatch, lane):
    output = "status=started\nroute_status=routed\npane_id=original:pane\n"
    runtime = HerdrRuntime(
        home=tmp_path,
        run=lambda argv, **kwargs: SimpleNamespace(returncode=0, stdout=validated(argv, output), stderr=""),
        choose=lambda *_: ("claude", "open"),
    )
    monkeypatch.setattr(runtime, "conversations", lambda: {"original:pane": "native-conversation"})
    from scripts.swarm.store import AgentRecord, SwarmConfig

    config = SwarmConfig("sw", str(tmp_path), max_eng=1, max_ci=0, code="a1b2c3")
    placed = runtime.spawn(config, lane, "agent@a1b2c3-0001", {"id": lane, "title": "Recovery"})
    record = tick_module.placed_record(AgentRecord("agent@a1b2c3-0001", lane, lane), placed)
    assert record.conversation_id == "native-conversation"


def test_resume_placement_carries_the_original_conversation(tmp_path):
    runtime, config, agent, seen = _resuming(tmp_path, "c0ffee")
    placed = runtime.resume(config, agent, "Continue the task")
    record = tick_module.placed_record(agent, placed)
    assert record.conversation_id == "c0ffee"
    assert placed.conversation_id == "c0ffee"


@pytest.mark.parametrize("lane", ["master", "eng"])
@pytest.mark.parametrize("reason", ["handoff", "limit", "failed"])
def test_dead_resume_records_why_a_fresh_agent_is_needed(store, monkeypatch, lane, reason):
    from scripts.swarm import dead_resume
    from scripts.swarm.tick import SpawnError

    ledger, runtime = tasks(("t1", "eng")), ResumingRuntime()
    tick_module.tick("sw", store, ledger, runtime, 1)
    agent = next(a for a in store.agents("sw") if a.lane == lane)
    agent = replace(agent, conversation_id="old-conversation", profile="engineer")
    store.put_agent("sw", agent)
    runtime.live.discard(agent.name)
    monkeypatch.setattr(context_recycle, "_used", lambda session: 600_000 if reason == "limit" else 240_000)
    if reason == "handoff":
        store.put_handoff("sw", agent.task, "Submitted handoff")
    if reason == "failed":

        def fail(config, original, text):
            assert original == agent
            raise SpawnError("native resume refused")

        monkeypatch.setattr(runtime, "resume", fail)

    assert dead_resume.reopen("sw", store, ledger, runtime, agent, 10) is False
    explanations = {
        "handoff": "a handoff was submitted",
        "limit": "the conversation reached its compact limit",
        "failed": "resume failed: native resume refused",
    }
    assert ledger.notes[-1] == f"Starting a fresh agent after {agent.name} ended because {explanations[reason]}."
    assert runtime.resumed == []


@pytest.mark.parametrize("lane", ["master", "eng"])
def test_confirmed_dead_process_reaches_resume_before_binding_retirement(store, monkeypatch, lane):
    from scripts.swarm.live_binding import assignment

    ledger, runtime = tasks(("t1", "eng")), ResumingRuntime()
    tick_module.tick("sw", store, ledger, runtime, 1)
    agent = next(a for a in store.agents("sw") if a.lane == lane)
    agent = replace(agent, conversation_id="old-conversation", profile="engineer")
    store.put_agent("sw", agent)
    runtime.live.discard(agent.name)

    def bindings(agents):
        return {
            a.name: {**assignment(a), "hooks": True} if a.name in runtime.live else {"process": False} for a in agents
        }

    monkeypatch.setattr(runtime, "bindings", bindings)
    monkeypatch.setattr(context_recycle, "_used", lambda session: 240_000)

    tick_module.tick("sw", store, ledger, runtime, STARTUP_GRACE_MS + 2)

    assert runtime.resumed == [agent]
    assert agent.pane_id not in runtime.closed
