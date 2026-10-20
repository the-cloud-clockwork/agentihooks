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


@pytest.mark.parametrize(
    ("state", "handoff", "reason"),
    [
        ("working", "", "retired"),
        ("finished", "", "finished"),
        ("working", "continuation", "handed off"),
    ],
)
def test_drop_preserves_history_and_clears_all_agent_indexes(store, state, handoff, reason):
    agent = started(store)
    store.put_agent("fixture", replace(agent, state=state))
    previous = json.loads(store.redis.hget(store.key("fixture", "agents"), agent.name))
    for part in ("desired-routes", "state-since", "retire-failures"):
        store.redis.hset(store.key("fixture", part), agent.name, "value")
    with (
        patch.object(store, "handoff", return_value=handoff),
        patch("scripts.swarm.store.time.time", return_value=1.234),
    ):
        store.drop_agent("fixture", agent.name)
    assert store.execution("fixture", agent.execution_id).state == "ended"
    assert [json.loads(raw) for raw in store.redis.lrange(store.key("fixture", "history"), 0, -1)] == [
        {**previous, "ended_at": 1234, "reason": reason}
    ]
    for part in ("agents", "desired-routes", "state-since", "retire-failures"):
        assert not store.redis.hexists(store.key("fixture", part), agent.name)
    assert store.names.entry(agent.name)["retired_at"] == 1234


def test_drop_retries_a_changed_projection_without_partial_history(store):
    agent = started(store)
    pipe_type = type(store.redis.pipeline())
    execute = pipe_type.execute
    calls = []

    def commit(pipe, *args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            row = json.loads(store.redis.hget(store.key("fixture", "agents"), agent.name))
            store.redis.hset(store.key("fixture", "agents"), agent.name, json.dumps({**row, "pane_id": "new-pane"}))
        return execute(pipe, *args, **kwargs)

    with patch.object(pipe_type, "execute", commit):
        store.drop_agent("fixture", agent.name, at=10)
    assert len(calls) == 2
    assert store.execution("fixture", agent.execution_id).state == "ended"
    history = store.redis.lrange(store.key("fixture", "history"), 0, -1)
    assert len(history) == 1
    assert json.loads(history[0])["pane_id"] == "new-pane"


def test_drop_refuses_persistent_contention_without_partial_changes(store):
    from redis.exceptions import WatchError

    from scripts.swarm.store import SwarmError

    agent = started(store)
    before = store.redis.hgetall(store.key("fixture", "agents"))
    with patch.object(type(store.redis.pipeline()), "execute", side_effect=WatchError) as commit:
        with pytest.raises(SwarmError, match="^agent registry kept changing; retirement was not committed$"):
            store.drop_agent("fixture", agent.name, at=20)
    assert commit.call_count == 5
    assert store.redis.hgetall(store.key("fixture", "agents")) == before
    assert store.execution("fixture", agent.execution_id) == agent
    assert store.redis.lrange(store.key("fixture", "history"), 0, -1) == []
    assert not store.names.entry(agent.name).get("retired_at")


def test_dropping_a_missing_legacy_agent_does_not_end_someone_elses_execution(store):
    agent = started(store)
    store.drop_agent("fixture", "missing", at=30)
    assert store.execution("fixture", agent.execution_id) == agent
    assert store.agents("fixture") == [agent]
    assert store.redis.lrange(store.key("fixture", "history"), 0, -1) == []
