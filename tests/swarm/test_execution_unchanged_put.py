from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from scripts.swarm import execution, fencing
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError

pytestmark = pytest.mark.unit


@pytest.fixture
def store() -> RedisStore:
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("fixture", "agentihooks", 1, 0))
    return store


@pytest.fixture
def agent(store: RedisStore) -> AgentRecord:
    return AgentRecord(store.next_name("fixture", "eng"), "eng", "task", seat="eng-1@fixture", started_at=123)


class _CountingLock:
    def __init__(self, lock: Any, acquires: list[int]) -> None:
        self._lock = lock
        self._acquires = acquires

    def __enter__(self) -> Any:
        self._acquires.append(1)
        return self._lock.__enter__()

    def __exit__(self, *exc: object) -> Any:
        return self._lock.__exit__(*exc)


def _watched(monkeypatch: pytest.MonkeyPatch) -> tuple[list[int], list[int]]:
    acquires: list[int] = []
    writes: list[int] = []
    real_writes = execution._writes
    real_write = fencing.write

    def counting_writes(slug: str) -> Any:
        return _CountingLock(real_writes(slug), acquires)

    def counting_write(store_arg: Any, slug: str, change: Any, fence: Any = None) -> Any:
        writes.append(1)
        return real_write(store_arg, slug, change, fence)

    monkeypatch.setattr(execution, "_writes", counting_writes)
    monkeypatch.setattr(fencing, "write", counting_write)
    return acquires, writes


@pytest.mark.parametrize("layout", ["document", "records"])
@pytest.mark.feature("execution-put-unchanged-skips-lock")
def test_an_unchanged_put_skips_the_write_lock_and_the_fenced_document_read(
    store: RedisStore, agent: AgentRecord, monkeypatch: pytest.MonkeyPatch, layout: str
) -> None:
    current = store.start_execution("fixture", agent)
    if layout == "records":
        fencing.migrate_records(store, "fixture", writers_upgraded=True)
    acquires, writes = _watched(monkeypatch)

    store.put_agent("fixture", current)

    assert acquires == []
    assert writes == []
    assert store.execution("fixture", current.execution_id) == current


@pytest.mark.parametrize("layout", ["document", "records"])
@pytest.mark.feature("execution-put-unchanged-skips-lock")
def test_a_changed_put_still_takes_the_locked_path(
    store: RedisStore, agent: AgentRecord, monkeypatch: pytest.MonkeyPatch, layout: str
) -> None:
    current = store.start_execution("fixture", agent)
    if layout == "records":
        fencing.migrate_records(store, "fixture", writers_upgraded=True)
    acquires, writes = _watched(monkeypatch)

    store.put_agent("fixture", replace(current, idle_ticks=2))

    assert len(acquires) >= 1
    assert len(writes) >= 1
    assert store.execution("fixture", current.execution_id).idle_ticks == 2


@pytest.mark.feature("execution-put-unchanged-skips-lock")
def test_an_unmanaged_legacy_put_still_takes_the_locked_path(
    store: RedisStore, agent: AgentRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    acquires, _ = _watched(monkeypatch)

    store.put_agent("fixture", agent)

    assert len(acquires) >= 1


@pytest.mark.feature("execution-put-unchanged-skips-lock")
def test_a_binding_or_uid_conflict_still_takes_the_locked_path(
    store: RedisStore, agent: AgentRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = store.start_execution("fixture", replace(agent, runtime_target={"host_id": "host"}))
    acquires, writes = _watched(monkeypatch)

    with pytest.raises(SwarmError, match="immutable"):
        store.put_agent("fixture", replace(current, runtime_target={"host_id": "host", "pid": 1}))

    assert len(acquires) >= 1
    assert len(writes) >= 1
