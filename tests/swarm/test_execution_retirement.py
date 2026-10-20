import json
from dataclasses import replace
from unittest.mock import patch

import pytest

from scripts.swarm.execution import ExecutionRegistry
from scripts.swarm.master_start import DEADLINE_MS
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
    ("state", "handoff", "reason", "terminal"),
    [
        ("working", "", "retired", "ended"),
        ("finished", "", "finished", "finished"),
        ("ended", "", "retired", "ended"),
        ("working", "continuation", "handed off", "ended"),
    ],
)
def test_drop_preserves_history_and_clears_all_agent_indexes(store, state, handoff, reason, terminal):
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
    assert store.execution("fixture", agent.execution_id).state == terminal
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
    from scripts.hive import reports

    if available:
        store.redis.set(reports.key("remote"), json.dumps({"names": []}))
    runtime = SimpleNamespace(
        live_names=lambda: {agent.name} if named else set(),
        reported=lambda current: reported if current == agent else True,
        observe=lambda current: SimpleNamespace(state=observed if current == agent else "unknown"),
    )
    with patch("scripts.hive.reports.host", return_value="remote" if remote else ""):
        actions = store.execution_registry.reconcile("fixture", runtime, 0)
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
    from scripts.hive import reports

    store.redis.set(reports.key("remote"), json.dumps({"names": [agent.name]}))
    runtime = SimpleNamespace(live_names=lambda: set(), reported=lambda _: False)
    with patch("scripts.hive.reports.host", return_value="remote"):
        assert store.execution_registry.reconcile("fixture", runtime, 0) == []
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


def test_drop_retries_when_its_execution_becomes_terminal_before_commit(store):
    from dataclasses import asdict

    agent = started(store)
    terminal = replace(agent, state="finished")
    pipe_type = type(store.redis.pipeline())
    execute = pipe_type.execute
    calls = []

    def commit(pipe, *args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            store.redis.hset(store.key("fixture", "executions"), agent.execution_id, json.dumps(asdict(terminal)))
        return execute(pipe, *args, **kwargs)

    with patch.object(pipe_type, "execute", commit):
        store.drop_agent("fixture", agent.name, at=40)
    assert len(calls) == 2
    assert store.execution("fixture", agent.execution_id) == terminal
    assert store.agents("fixture") == []
    assert store.redis.llen(store.key("fixture", "history")) == 1


def test_orphan_ending_keeps_a_concurrent_execution_update(store):
    from dataclasses import asdict

    agent = started(store)
    changed = replace(agent, idle_ticks=1)
    store.redis.hdel(store.key("fixture", "agents"), agent.name)
    pipe_type = type(store.redis.pipeline())
    execute = pipe_type.execute

    def commit(pipe, *args, **kwargs):
        store.redis.hset(store.key("fixture", "executions"), agent.execution_id, json.dumps(asdict(changed)))
        return execute(pipe, *args, **kwargs)

    with patch.object(pipe_type, "execute", commit):
        assert store.execution_registry.end_orphan("fixture", agent) is False
    assert store.execution("fixture", agent.execution_id) == changed
    assert store.agents("fixture") == []


@pytest.mark.parametrize(("state", "projected"), [("working", True), ("finished", False)])
def test_reconciliation_skips_unavailable_runtime_reads_for_non_orphans(store, state, projected):
    from types import SimpleNamespace
    from unittest.mock import Mock

    agent = started(store)
    agent = replace(agent, state=state)
    store.put_agent("fixture", agent)
    if not projected:
        store.redis.hdel(store.key("fixture", "agents"), agent.name)
    runtime = SimpleNamespace(live_names=lambda: set(), reported=Mock(side_effect=RuntimeError("runtime unavailable")))
    assert store.execution_registry.reconcile("fixture", runtime, 0) == []
    assert store.execution("fixture", agent.execution_id) == agent
    runtime.reported.assert_not_called()


def test_a_recorded_seat_does_not_hide_a_later_dead_orphan(store):
    from types import SimpleNamespace

    recorded = started(store)
    orphan = store.start_execution(
        "fixture", AgentRecord(store.next_name("fixture", "eng"), "eng", "next-task", seat="eng-2@fixture")
    )
    store.redis.hdel(store.key("fixture", "agents"), orphan.name)
    runtime = SimpleNamespace(live_names=lambda: set(), reported=lambda _: False)
    assert store.execution_registry.reconcile("fixture", runtime, 0) == [
        f"ended orphan execution {orphan.execution_id} for {orphan.name}"
    ]
    assert store.execution("fixture", recorded.execution_id) == recorded
    assert store.execution("fixture", orphan.execution_id) == replace(orphan, state="ended")
    assert store.agents("fixture") == [recorded]


def starting_master(store, slug, dropped=True):
    orphan = store.start_execution(
        slug,
        AgentRecord(
            store.next_name(slug, "master"), "master", "master", seat=f"master@{slug}", started_at=1, state="starting"
        ),
    )
    if dropped:
        store.redis.hdel(store.key(slug, "agents"), orphan.name)
    return orphan


@pytest.mark.parametrize(("offset", "ended"), [(0, True), (-1, False)])
def test_tick_ends_a_starting_master_orphan_only_past_its_startup_window(store, offset, ended):
    from scripts.swarm.tick import tick
    from tests.swarm.test_tick import FakeLedger, FakeRuntime

    store.create(SwarmConfig("sw", "/repo", max_eng=0, max_ci=0, state="paused"))
    orphan = starting_master(store, "sw")
    runtime = FakeRuntime()
    tick("sw", store, FakeLedger([]), runtime, 1 + DEADLINE_MS + offset)
    assert store.execution("sw", orphan.execution_id) == replace(orphan, state="ended" if ended else "starting")
    assert orphan.name not in runtime.killed


@pytest.mark.parametrize(
    ("offset", "projected", "named", "reported", "ended"),
    [
        (0, False, False, False, True),
        (1, False, False, False, True),
        (-1, False, False, False, False),
        (0, True, False, False, False),
        (0, False, True, False, False),
        (0, False, False, True, False),
    ],
)
def test_reconciliation_ends_a_starting_orphan_only_when_absent_dead_and_past_startup(
    store, offset, projected, named, reported, ended
):
    from types import SimpleNamespace

    orphan = starting_master(store, "fixture", dropped=not projected)
    runtime = SimpleNamespace(
        live_names=lambda: {orphan.name} if named else set(),
        reported=lambda current: reported,
    )
    actions = store.execution_registry.reconcile("fixture", runtime, 1 + DEADLINE_MS + offset)
    assert actions == ([f"ended orphan execution {orphan.execution_id} for {orphan.name}"] if ended else [])
    assert store.execution("fixture", orphan.execution_id) == replace(orphan, state="ended" if ended else "starting")


def test_orphan_ending_ends_a_starting_execution(store):
    orphan = starting_master(store, "fixture")
    assert store.execution_registry.end_orphan("fixture", orphan) is True
    assert store.execution("fixture", orphan.execution_id) == replace(orphan, state="ended")
