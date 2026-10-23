import json
from unittest.mock import MagicMock

import pytest

from scripts.swarm.execution import ARCHIVE, ExecutionRegistry
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig


@pytest.fixture
def store():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("fixture", "agentihooks", 1, 0))
    return store


def started(store, seat, previous=""):
    return store.start_execution(
        "fixture", AgentRecord(store.next_name("fixture", "eng"), "eng", "task", seat=seat), previous
    )


def history(store):
    first = started(store, "eng-1@fixture")
    store.drop_agent("fixture", first.name)
    second = started(store, "eng-1@fixture", first.execution_id)
    store.drop_agent("fixture", second.name)
    third = started(store, "eng-1@fixture", second.execution_id)
    other = started(store, "eng-2@fixture")
    store.drop_agent("fixture", other.name)
    return first, second, third, other


def test_archive_moves_only_rows_no_live_reader_returns(store):
    first, second, third, other = history(store)
    registry = store.execution_registry
    agents, occupants = registry.agents("fixture"), registry.occupants("fixture")
    records = sorted(registry.records("fixture"), key=lambda a: a.execution_id)

    assert registry.archive("fixture") == 2

    assert set(store.redis.hkeys(store.key("fixture", "executions"))) == {third.execution_id, other.execution_id}
    assert set(store.redis.hkeys(store.key("fixture", ARCHIVE))) == {first.execution_id, second.execution_id}
    assert registry.agents("fixture") == agents
    assert registry.occupants("fixture") == occupants
    assert sorted(registry.records("fixture"), key=lambda a: a.execution_id) == records
    assert store.execution("fixture", first.execution_id).name == first.name
    assert registry.managed("fixture", second.name)
    assert not registry.managed("fixture", "nobody@fixture")
    assert registry.archive("fixture") == 0


def test_set_pane_still_finds_an_archived_attempt(store):
    first, *_ = history(store)
    store.execution_registry.archive("fixture")

    store.execution_registry.set_pane("fixture", first.execution_id, "pane-9")

    assert store.execution("fixture", first.execution_id).pane_id == "pane-9"
    assert not store.redis.hexists(store.key("fixture", ARCHIVE), first.execution_id)


@pytest.mark.parametrize("archived", [False, True])
def test_set_pane_on_a_replaced_run_keeps_its_dropped_agent_out_of_the_live_list(store, archived):
    first, second, third, other = history(store)
    if archived:
        store.execution_registry.archive("fixture")
    live = store.redis.hgetall(store.key("fixture", "agents"))

    store.execution_registry.set_pane("fixture", first.execution_id, "pane-9")

    assert store.execution("fixture", first.execution_id).pane_id == "pane-9"
    assert store.redis.hgetall(store.key("fixture", "agents")) == live
    assert [agent.name for agent in store.agents("fixture")] == [third.name]


def test_set_pane_retries_when_its_agent_is_dropped_during_the_write(store, monkeypatch):
    current = started(store, "eng-1@fixture")
    original = store.redis.pipeline
    drops = []

    def pipeline(*args, **kwargs):
        pipe = original(*args, **kwargs)
        hexists = pipe.hexists

        def racing(name, key):
            if not drops:
                drops.append(key)
                store.drop_agent("fixture", key)
            return hexists(name, key)

        monkeypatch.setattr(pipe, "hexists", racing)
        return pipe

    monkeypatch.setattr(store.redis, "pipeline", pipeline)

    store.execution_registry.set_pane("fixture", current.execution_id, "pane-9")

    assert drops == [current.name]
    assert store.execution("fixture", current.execution_id).pane_id == "pane-9"
    assert not store.redis.hexists(store.key("fixture", "agents"), current.name)


def test_archive_keeps_every_row_of_a_live_name_and_seat_head_ties(store):
    agent = AgentRecord(store.next_name("fixture", "eng"), "eng", "task", seat="eng-3@fixture")
    one = store.start_execution("fixture", agent)
    two = store.start_execution("fixture", agent, one.execution_id)
    three = store.start_execution("fixture", agent, two.execution_id)
    tie = {**json.loads(store.redis.hget(store.key("fixture", "executions"), three.execution_id))}
    tie.update(name="gone@fixture", execution_id="exe-tie")
    store.redis.hset(store.key("fixture", "executions"), "exe-tie", json.dumps(tie))
    registry = store.execution_registry
    agents, occupants = registry.agents("fixture"), registry.occupants("fixture")
    assert [a.name for a in agents] == [agent.name]

    assert registry.archive("fixture") == 0

    assert set(store.redis.hkeys(store.key("fixture", "executions"))) == {
        one.execution_id,
        two.execution_id,
        three.execution_id,
        "exe-tie",
    }
    assert registry.agents("fixture") == agents
    assert registry.occupants("fixture") == occupants


def test_archive_keeps_rows_without_a_seat_or_generation(store):
    history(store)
    key = store.key("fixture", "executions")
    store.redis.hset(key, "legacy", json.dumps({"name": "old@fixture"}))
    store.redis.hset(key, "nameless", json.dumps({"seat": "eng-1@fixture", "generation": 0}))

    store.execution_registry.archive("fixture")

    assert store.redis.hexists(key, "legacy")
    assert store.redis.hexists(key, "nameless")


def test_reconcile_archives_dead_rows(store):
    first, second, *_ = history(store)
    runtime = MagicMock()
    runtime.live_names.return_value = set()

    ExecutionRegistry(store).reconcile("fixture", runtime, 0)

    assert set(store.redis.hkeys(store.key("fixture", ARCHIVE))) == {first.execution_id, second.execution_id}
