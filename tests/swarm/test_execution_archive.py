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


def test_reconcile_archives_dead_rows(store):
    first, second, *_ = history(store)
    runtime = MagicMock()
    runtime.live_names.return_value = set()

    ExecutionRegistry(store).reconcile("fixture", runtime, 0)

    assert set(store.redis.hkeys(store.key("fixture", ARCHIVE))) == {first.execution_id, second.execution_id}
