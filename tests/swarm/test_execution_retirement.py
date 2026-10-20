import json
from dataclasses import replace
from unittest.mock import patch

import pytest

from scripts.swarm.execution import ExecutionRegistry
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig


@pytest.fixture
def store():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("fixture", "agentihooks", 1, 0))
    assert isinstance(store.execution_registry, ExecutionRegistry)
    return store


def started(store):
    return store.start_execution(
        "fixture", AgentRecord(store.next_name("fixture", "eng"), "eng", "task", seat="eng-1@fixture")
    )


def test_drop_ends_execution_in_the_transaction_that_removes_its_agent(store):
    agent = started(store)
    pipe_type = type(store.redis.pipeline())
    execute = pipe_type.execute
    observed = []

    def commit(pipe, *args, **kwargs):
        before = store.redis.hexists(store.key("fixture", "agents"), agent.name)
        result = execute(pipe, *args, **kwargs)
        after = store.redis.hexists(store.key("fixture", "agents"), agent.name)
        if before and not after:
            observed.append(store.execution("fixture", agent.execution_id).state)
        return result

    with patch.object(pipe_type, "execute", commit):
        store.drop_agent("fixture", agent.name, at=789)

    assert store.execution("fixture", agent.execution_id) == replace(agent, state="ended")
    assert store.agents("fixture") == []
    assert observed == ["ended"]
    history = [json.loads(raw) for raw in store.redis.lrange(store.key("fixture", "history"), 0, -1)]
    assert len(history) == 1
    assert history[0]["ended_at"] == 789
    assert history[0]["reason"] == "retired"
