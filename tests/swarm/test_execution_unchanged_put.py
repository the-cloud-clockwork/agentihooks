from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
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


@pytest.mark.feature("execution-put-unchanged-skips-lock")
def test_invalid_rejects_an_unknown_runtime_backend(agent: AgentRecord) -> None:
    assert execution._invalid("fixture", replace(agent, runtime_backend="docker")) is True


@pytest.mark.feature("execution-put-unchanged-skips-lock")
def test_invalid_accepts_a_valid_kubernetes_target(agent: AgentRecord) -> None:
    pod = replace(agent, runtime_backend="kubernetes", runtime_target={"pod_namespace": "ns", "pod_name": "pod"})

    assert execution._invalid("fixture", pod) is False


@pytest.mark.feature("execution-put-unchanged-skips-lock")
def test_invalid_rejects_an_incomplete_kubernetes_target(agent: AgentRecord) -> None:
    pod = replace(agent, runtime_backend="kubernetes", runtime_target={"pod_namespace": "ns"})

    assert execution._invalid("fixture", pod) is True


@pytest.mark.feature("execution-put-unchanged-skips-lock")
def test_invalid_rejects_a_seat_outside_the_swarm(agent: AgentRecord) -> None:
    assert execution._invalid("fixture", replace(agent, seat="eng-1@other")) is True


@pytest.mark.feature("execution-put-unchanged-skips-lock")
def test_binding_conflict_flags_a_changed_identity_field() -> None:
    assert execution._binding_conflict({"host_id": "host"}, {"host_id": "host", "pid": 1}) is True


@pytest.mark.feature("execution-put-unchanged-skips-lock")
def test_an_unchanged_put_forces_a_locally_finished_record_through_the_fast_path(
    store: RedisStore, agent: AgentRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = store.start_execution("fixture", agent)
    fencing.write(store, "fixture", lambda doc: doc["executions"][current.execution_id].update(state="finished"))
    acquires, writes = _watched(monkeypatch)

    store.put_agent("fixture", current)

    assert acquires == []
    assert writes == []
    assert store.execution("fixture", current.execution_id).state == "finished"


@pytest.mark.feature("execution-put-unchanged-skips-lock")
def test_an_unchanged_put_syncs_launch_binding_fields_on_a_read_before_ack(
    store: RedisStore, agent: AgentRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = store.start_execution("fixture", agent)
    bound_target = {"host_id": "host", "process_namespace": "ns", "pid": 5, "pid_start": 1000}
    fencing.write(
        store, "fixture", lambda doc: doc["executions"][current.execution_id].update(runtime_target=bound_target)
    )
    acquires, writes = _watched(monkeypatch)

    store.put_agent("fixture", replace(current, runtime_target={"host_id": "host"}))

    assert acquires == []
    assert writes == []
    assert store.execution("fixture", current.execution_id).runtime_target == bound_target


@pytest.mark.feature("execution-put-unchanged-skips-lock")
def test_an_unchanged_put_with_a_bound_pod_uid_still_takes_the_locked_path(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    pod_agent = AgentRecord(
        store.next_name("fixture", "eng"),
        "eng",
        "task",
        seat="eng-1@fixture",
        started_at=123,
        runtime_backend="kubernetes",
        runtime_target={"pod_namespace": "ns", "pod_name": "pod", "pod_uid": "uid-1"},
    )
    current = store.start_execution("fixture", pod_agent)
    acquires, _ = _watched(monkeypatch)

    store.put_agent("fixture", current)

    assert len(acquires) >= 1


@pytest.mark.feature("execution-put-unchanged-skips-lock")
def test_a_fenced_head_with_no_matching_execution_row_still_takes_the_locked_path(
    store: RedisStore, agent: AgentRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = store.start_execution("fixture", agent)
    calls: list[str] = []

    class _MissingRow:
        def record(self, slug: str, section: str, key: str) -> Any:
            if section == fencing.BY_SEAT:
                return SimpleNamespace(value=current.execution_id)
            return None

    def fake_records(store_arg: Any, slug: str) -> Any:
        calls.append(slug)
        return _MissingRow()

    monkeypatch.setattr(execution.fencing, "records", fake_records)
    acquires, _ = _watched(monkeypatch)

    store.put_agent("fixture", current)

    assert calls == ["fixture"]
    assert len(acquires) >= 1
