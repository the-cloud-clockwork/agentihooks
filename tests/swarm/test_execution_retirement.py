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


@pytest.mark.parametrize("live", [False, True])
def test_tick_ends_a_working_orphan_only_when_its_process_is_gone(store, live):
    from scripts.swarm.tick import tick
    from tests.swarm.test_tick import FakeLedger, FakeRuntime

    store.create(SwarmConfig("sw", "/repo", max_eng=0, max_ci=0, state="paused"))
    agent = store.start_execution(
        "sw", AgentRecord(store.next_name("sw", "eng"), "eng", "task", seat="eng-1@sw", started_at=1)
    )
    store.redis.hdel(store.key("sw", "agents"), agent.name)
    runtime = FakeRuntime()
    if live:
        runtime.live.add(agent.name)
    tick("sw", store, FakeLedger([]), runtime, 100000)
    assert store.execution("sw", agent.execution_id) == replace(agent, state="working" if live else "ended")
    assert agent.name not in {a.name for a in store.agents("sw")}
    assert agent.name not in runtime.killed


@pytest.mark.parametrize(
    "case",
    [
        ("working", False, False, False, False, True, "local", "unknown", True),
        ("finished", False, False, False, False, True, "local", "unknown", False),
        ("working", True, False, False, False, True, "local", "unknown", False),
        ("working", False, True, False, False, True, "local", "unknown", False),
        ("working", False, False, True, False, True, "local", "unknown", False),
        ("working", False, False, False, True, False, "local", "unknown", False),
        ("working", False, False, False, True, True, "local", "unknown", True),
        ("working", False, False, False, False, True, "kubernetes", "unknown", False),
        ("working", False, False, False, False, True, "kubernetes", "dead", True),
    ],
)
def test_reconciliation_requires_an_absent_agent_and_a_dead_process(store, case):
    state, projected, named, reported, remote, available, backend, observed, ended = case
    from types import SimpleNamespace

    from tests.swarm.test_execution_identity import target

    agent = store.start_execution(
        "fixture",
        AgentRecord(
            store.next_name("fixture", "eng"),
            "eng",
            "task",
            seat="eng-1@fixture",
            state=state,
            runtime_backend=backend,
            runtime_target=target() if backend == "kubernetes" else {},
        ),
    )
    if not projected:
        store.redis.hdel(store.key("fixture", "agents"), agent.name)
    runtime = SimpleNamespace(
        live_names=lambda: {agent.name} if named else set(),
        reported=lambda _: reported,
        observe=lambda _: SimpleNamespace(state=observed),
    )
    with (
        patch("scripts.hive.reports.host", return_value="remote" if remote else ""),
        patch("scripts.hive.reports.Reports.read", return_value={"names": []} if available else None),
    ):
        actions = store.execution_registry.reconcile("fixture", runtime)
    expected = [f"ended orphan execution {agent.execution_id} for {agent.name}"] if ended else []
    assert actions == expected
    expected_agent = replace(agent, state="ended") if ended else agent
    assert store.execution("fixture", agent.execution_id) == expected_agent
    assert bool(store.redis.hexists(store.key("fixture", "agents"), agent.name)) == projected


def test_orphan_ending_does_not_touch_a_replaced_execution_or_restored_agent(store):
    first = started(store)
    store.redis.hdel(store.key("fixture", "agents"), first.name)
    replacement = store.start_execution(
        "fixture", AgentRecord(first.name, "eng", "task", seat=first.seat), first.execution_id
    )
    assert store.execution_registry.end_orphan("fixture", first) is False
    assert store.execution_registry.end_orphan("fixture", replacement) is False
    assert store.execution("fixture", first.execution_id) == first
    assert store.execution("fixture", replacement.execution_id) == replacement


def test_orphan_ending_refuses_a_projection_restored_during_the_transaction(store):
    agent = started(store)
    projection = store.redis.hget(store.key("fixture", "agents"), agent.name)
    store.redis.hdel(store.key("fixture", "agents"), agent.name)
    pipe_type = type(store.redis.pipeline())
    execute = pipe_type.execute

    def commit(pipe, *args, **kwargs):
        store.redis.hset(store.key("fixture", "agents"), agent.name, projection)
        return execute(pipe, *args, **kwargs)

    with patch.object(pipe_type, "execute", commit):
        assert store.execution_registry.end_orphan("fixture", agent) is False
    assert store.execution("fixture", agent.execution_id) == agent
    assert store.agents("fixture") == [agent]


def test_a_remote_process_reported_after_the_live_snapshot_is_preserved(store):
    from types import SimpleNamespace

    agent = started(store)
    store.redis.hdel(store.key("fixture", "agents"), agent.name)
    runtime = SimpleNamespace(live_names=lambda: set(), reported=lambda _: False)
    with patch("scripts.hive.reports.host", return_value="remote"), patch(
        "scripts.hive.reports.Reports.read", return_value={"names": [agent.name]}
    ):
        assert store.execution_registry.reconcile("fixture", runtime) == []
    assert store.execution("fixture", agent.execution_id) == agent


def test_orphan_ending_preserves_terminal_executions_and_absent_replacements(store):
    first = started(store)
    replacement = store.start_execution(
        "fixture", AgentRecord(first.name, "eng", "task", seat=first.seat), first.execution_id
    )
    store.redis.hdel(store.key("fixture", "agents"), first.name)
    assert store.execution_registry.end_orphan("fixture", first) is False
    assert store.execution("fixture", first.execution_id) == first
    assert store.execution("fixture", replacement.execution_id) == replacement
    terminal = replace(replacement, state="finished")
    store.put_agent("fixture", terminal)
    store.redis.hdel(store.key("fixture", "agents"), first.name)
    assert store.execution_registry.end_orphan("fixture", terminal) is False
    assert store.execution("fixture", terminal.execution_id) == terminal
