"""The managed name check reads an index keyed by agent name and runs outside the swarm write lock."""

import threading
from collections.abc import Sequence
from typing import Any

import pytest

from scripts.swarm import execution, fencing
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.tick.adapters.records_redis import RedisRecords

pytestmark = pytest.mark.unit


@pytest.fixture
def store() -> RedisStore:
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("fixture", "agentihooks", 1, 0))
    return store


def started(store: RedisStore, seat: str, previous: str = "") -> AgentRecord:
    return store.start_execution(
        "fixture", AgentRecord(store.next_name("fixture", "eng"), "eng", "task", seat=seat), previous
    )


def history(store: RedisStore) -> tuple[AgentRecord, AgentRecord, AgentRecord]:
    first = started(store, "eng-1@fixture")
    store.drop_agent("fixture", first.name)
    second = started(store, "eng-1@fixture", first.execution_id)
    store.drop_agent("fixture", second.name)
    third = started(store, "eng-1@fixture", second.execution_id)
    return first, second, third


def archive_reads(monkeypatch: pytest.MonkeyPatch) -> list[Sequence[str]]:
    reads: list[Sequence[str]] = []
    sections = RedisRecords.sections

    def counted(self: RedisRecords, slug: str, names: Sequence[str]) -> dict[str, Any]:
        if fencing.ARCHIVE in names:
            reads.append(names)
        return sections(self, slug, names)

    monkeypatch.setattr(RedisRecords, "sections", counted)
    return reads


@pytest.mark.parametrize("archived_first", [False, True])
def test_managed_reads_the_archived_name_index_and_never_the_archive(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, archived_first: bool
) -> None:
    first, second, third = history(store)
    registry = store.execution_registry
    if archived_first:
        assert registry.archive("fixture") == 2
    fencing.migrate_records(store, "fixture", writers_upgraded=True)
    if not archived_first:
        assert registry.archive("fixture") == 2
    reads = archive_reads(monkeypatch)
    assert registry.managed("fixture", first.name) is True
    assert registry.managed("fixture", second.name) is True
    assert registry.managed("fixture", third.name) is True
    assert registry.managed("fixture", first.name[:-1]) is False
    assert registry.managed("fixture", "nobody@fixture") is False
    assert reads == []


def test_a_swarm_migrated_before_the_index_builds_it_once(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> None:
    first, second, third = history(store)
    registry = store.execution_registry
    fencing.migrate_records(store, "fixture", writers_upgraded=True)
    registry.archive("fixture")
    store.redis.delete(RedisRecords(store.redis).key("fixture", fencing.ARCHIVED_NAMES))
    reads = archive_reads(monkeypatch)
    assert registry.managed("fixture", "nobody@fixture") is False
    assert len(reads) == 1
    assert registry.managed("fixture", first.name) is True
    assert registry.managed("fixture", second.name) is True
    assert registry.managed("fixture", "someone@fixture") is False
    assert len(reads) == 1
    index = fencing.records(store, "fixture")
    assert index.record("fixture", fencing.ARCHIVED_NAMES, first.name).value == first.execution_id
    assert index.record("fixture", fencing.ARCHIVED_NAMES, third.name) is None


def test_a_restore_that_drops_an_archived_name_drops_it_from_the_index(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    fencing.migrate_records(store, "fixture", writers_upgraded=True)
    saved = fencing.read(store, "fixture")
    first, second, third = history(store)
    registry = store.execution_registry
    registry.archive("fixture")
    assert registry.managed("fixture", first.name) is True
    fencing.replace(store, "fixture", saved)
    reads = archive_reads(monkeypatch)
    assert registry.managed("fixture", first.name) is False
    assert registry.managed("fixture", third.name) is False
    assert reads == []


def lock_free(slug: str) -> bool:
    found: list[bool] = []

    def attempt() -> None:
        lock = execution._writes(slug)
        taken = lock.acquire(blocking=False)
        if taken:
            lock.release()
        found.append(taken)

    thread = threading.Thread(target=attempt)
    thread.start()
    thread.join()
    return found[0]


def test_put_checks_managed_names_outside_the_swarm_write_lock(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = store.execution_registry
    managed = registry.managed
    probes: list[tuple[bool, str]] = []

    def probing(slug: str, name: str) -> bool:
        probes.append((lock_free(slug), name))
        return managed(slug, name)

    monkeypatch.setattr(registry, "managed", probing)
    agent = AgentRecord(store.next_name("fixture", "eng"), "eng", "task", seat="eng-1@fixture", started_at=123)
    store.put_agent("fixture", agent)
    assert probes == [(True, agent.name)]
    assert [row.name for row in store.agents("fixture")] == [agent.name]
