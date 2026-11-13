import json
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.fixture
def store() -> Any:
    import fakeredis

    from scripts.swarm.store import RedisStore, SwarmConfig

    result = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    result.create(SwarmConfig("fixture", "agentihooks", 1, 0))
    return result


def test_migration_moves_history_outside_fenced_writes(store: Any) -> None:
    from scripts.swarm import fencing

    rows = {str(index): {"execution_id": str(index), "payload": "x" * 2048} for index in range(4017)}
    fencing.write(store, "fixture", lambda doc: doc["archive"].update(rows))
    before = fencing._states(store).get("fixture")
    assert len(before.value) > 8_000_000

    fencing.migrate_archive(store, "fixture", writers_upgraded=True)

    entry = fencing._states(store).get("fixture")
    assert len(entry.value) < 1_000_000
    assert json.loads(entry.value)["archive"] == {}
    assert fencing.read(store, "fixture")["archive"] == rows
    fencing.write(store, "fixture", lambda doc: doc.update(epoch=1))
    assert fencing.read(store, "fixture")["archive"] == rows
    assert fencing.read(store, "fixture")["epoch"] == 1


def test_migration_requires_the_reader_rollout(store: Any) -> None:
    from scripts.swarm import fencing
    from scripts.swarm.store import SwarmError

    fencing.write(store, "fixture", lambda doc: doc["archive"].update({"old": {"name": "old"}}))
    before = fencing._states(store).get("fixture")
    with pytest.raises(SwarmError) as caught:
        fencing.migrate_archive(store, "fixture")
    assert str(caught.value) == "upgrade every live fencing writer before migrating the archive"
    assert fencing._states(store).get("fixture") == before
    assert "archive_ref" not in json.loads(before.value)


def test_migrated_generic_writes_never_load_history(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.events.redis_state import RedisStateStore
    from scripts.swarm import fencing

    fencing.write(store, "fixture", lambda doc: doc["archive"].update({"old": {"name": "old"}}))
    fencing.migrate_archive(store, "fixture", writers_upgraded=True)
    original = RedisStateStore.get

    def current_only(self: Any, key: str) -> Any:
        assert ":archive:" not in key
        return original(self, key)

    monkeypatch.setattr(RedisStateStore, "get", current_only)
    fencing.write(store, "fixture", lambda doc: doc.update(epoch=2))
    assert json.loads(fencing._states(store).get("fixture").value)["epoch"] == 2


def test_inline_overlay_is_preserved_after_external_migration(store: Any) -> None:
    from scripts.swarm import fencing

    fencing.write(store, "fixture", lambda doc: doc["archive"].update({"old": {"name": "old"}}))
    fencing.migrate_archive(store, "fixture", writers_upgraded=True)
    states = fencing._states(store)
    entry = states.get("fixture")
    inline = json.loads(entry.value)
    inline["archive"] = {"old": {"name": "changed"}, "new": {"name": "new"}}
    states.update("fixture", json.dumps(inline).encode(), entry.revision)
    assert fencing.read(store, "fixture")["archive"] == inline["archive"]
    fencing.write(store, "fixture", lambda doc: doc.update(epoch=3))
    assert fencing.read(store, "fixture")["archive"] == inline["archive"]
    assert json.loads(states.get("fixture").value)["archive"] == {}


def test_historical_execution_readers_and_pane_restore_survive_migration(store: Any) -> None:
    from scripts.swarm import fencing
    from tests.swarm.test_execution_archive import history

    first, second, third, other = history(store)
    registry = store.execution_registry
    registry.archive("fixture")
    records = registry.records("fixture")
    occupants = registry.occupants("fixture")
    agents = registry.agents("fixture")
    fencing.migrate_archive(store, "fixture", writers_upgraded=True)
    assert registry.records("fixture") == records
    assert registry.occupants("fixture") == occupants
    assert registry.agents("fixture") == agents
    assert registry.managed("fixture", first.name)
    assert not registry.managed("fixture", "nobody@fixture")
    assert store.execution("fixture", second.execution_id) == next(
        row for row in records if row.execution_id == second.execution_id
    )
    registry.set_pane("fixture", first.execution_id, "old-pane")
    assert json.loads(fencing._states(store).get("fixture").value)["archive_ref"].startswith("fixture:archive:")
    assert store.execution("fixture", first.execution_id).pane_id == "old-pane"
    assert first.execution_id not in fencing.read(store, "fixture")["archive"]
    assert registry.occupants("fixture") == occupants
    assert registry.agents("fixture") == agents
    registry.archive("fixture")
    assert store.execution("fixture", first.execution_id).pane_id == "old-pane"
    assert {row.execution_id for row in registry.records("fixture")} == {
        first.execution_id,
        second.execution_id,
        third.execution_id,
        other.execution_id,
    }


def test_archived_name_cannot_start_in_another_seat(store: Any) -> None:
    from scripts.swarm import execution, fencing
    from scripts.swarm.store import AgentRecord, SwarmError
    from tests.swarm.test_execution_archive import history

    first, _, _, _ = history(store)
    store.execution_registry.archive("fixture")
    fencing.migrate_archive(store, "fixture", writers_upgraded=True)
    candidate = AgentRecord(first.name, "eng", "task", seat="eng-3@fixture")
    with pytest.raises(SwarmError) as caught:
        execution.ExecutionRegistry(store).start("fixture", candidate, "")
    assert str(caught.value) == "canonical agent already belongs to another execution seat"


def test_empty_archive_and_snapshots_round_trip(store: Any) -> None:
    from scripts.swarm import fencing

    fencing.migrate_archive(store, "fixture", writers_upgraded=True)
    empty = fencing.read(store, "fixture")
    assert empty["archive"] == {}
    fencing.change_with_archive(store, "fixture", empty, lambda doc: doc["archive"].update({"old": {"name": "old"}}))
    fencing.hydrate_archive(store, empty)
    fencing.replace(store, "fixture", empty)
    saved = fencing.read(store, "fixture")
    assert saved["archive"] == {"old": {"name": "old"}}
    fencing.replace(store, "fixture", {**saved, "archive": {}, "archive_ref": ""})
    assert fencing.read(store, "fixture")["archive"] == {}
    fencing.replace(store, "fixture", saved)
    assert fencing.read(store, "fixture")["archive"] == saved["archive"]


def test_archive_reference_missing_fails_closed(store: Any) -> None:
    from scripts.swarm import fencing
    from scripts.swarm.store import SwarmError

    with pytest.raises(SwarmError) as caught:
        fencing._archive_rows(store, "fixture:archive:missing")
    assert str(caught.value) == "referenced execution archive is unavailable"
    fencing.write(store, "fixture", lambda doc: doc.update(archive_ref="missing"))
    with pytest.raises(SwarmError) as read_failure:
        fencing.read(store, "fixture")
    assert str(read_failure.value) == "referenced execution archive is unavailable"


def test_self_contained_snapshot_restores_without_source_chunks(store: Any) -> None:
    from scripts.swarm import fencing

    fencing.write(store, "fixture", lambda doc: doc["archive"].update({"old": {"name": "old"}}))
    fencing.migrate_archive(store, "fixture", writers_upgraded=True)
    saved = fencing.read(store, "fixture")
    store.redis.flushdb()
    fencing.replace(store, "fixture", saved)
    assert fencing.read(store, "fixture")["archive"] == saved["archive"]
    raw = json.loads(fencing._states(store).get("fixture").value)
    assert raw["archive"] == {}
    assert raw["archive_ref"].startswith("fixture:archive:")


def test_archive_chunks_retain_only_changes(store: Any) -> None:
    from scripts.swarm import fencing

    rows = {str(index): {"name": str(index), "payload": "x" * 2048} for index in range(130)}
    fencing.write(store, "fixture", lambda doc: doc["archive"].update(rows))
    fencing.migrate_archive(store, "fixture", writers_upgraded=True)
    initial = fencing.read(store, "fixture")
    original = initial["archive_ref"]
    fencing.write(store, "fixture", lambda doc: doc["archive"].update({"new": {"name": "new"}}))
    saved = fencing.read(store, "fixture")
    reference = saved["archive_ref"]
    assert reference != original
    assert reference.startswith("fixture:archive:")
    latest = json.loads(fencing._states(store).get(reference).value)
    assert latest == {"parent": original, "rows": {"new": {"name": "new"}}, "removed": []}
    total = 0
    count = 0
    while reference:
        chunk = json.loads(fencing._states(store).get(reference).value)
        assert len(chunk["rows"]) <= 64
        total += len(chunk["rows"])
        count += 1
        reference = chunk["parent"]
    assert total == 131
    assert count == 4
    assert saved["archive"] == {**rows, "new": {"name": "new"}}
    fencing.replace(store, "fixture", initial)
    assert fencing.read(store, "fixture")["archive"] == rows


def test_active_updates_and_idle_archive_pass_never_load_history(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.events.redis_state import RedisStateStore
    from scripts.swarm import fencing
    from tests.swarm.test_execution_archive import history

    first, second, third, other = history(store)
    registry = store.execution_registry
    registry.archive("fixture")
    fencing.migrate_archive(store, "fixture", writers_upgraded=True)
    original = RedisStateStore.get

    def current_only(self: Any, key: str) -> Any:
        assert ":archive:" not in key
        return original(self, key)

    monkeypatch.setattr(RedisStateStore, "get", current_only)
    registry.update("fixture", third)
    assert registry.archive("fixture") == 0
    assert registry.occupants("fixture")["eng-1@fixture"].execution_id == third.execution_id


def test_cas_retry_never_publishes_uncommitted_archive_chunks(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.events.bus import StateConflict
    from scripts.events.redis_state import RedisStateStore
    from scripts.swarm import fencing

    fencing.write(store, "fixture", lambda doc: doc["archive"].update({"old": {"name": "old"}}))
    before = fencing.read(store, "fixture")
    original = RedisStateStore.update
    attempts = []

    def conflict_once(self: Any, key: str, value: bytes, revision: int) -> int:
        if key == "fixture" and not attempts:
            attempts.append(value)
            assert fencing.read(store, "fixture") == before
            raise StateConflict("concurrent writer")
        return original(self, key, value, revision)

    monkeypatch.setattr(RedisStateStore, "update", conflict_once)
    fencing.migrate_archive(store, "fixture", writers_upgraded=True)
    assert len(attempts) == 1
    assert fencing.read(store, "fixture")["archive"] == before["archive"]
    assert json.loads(fencing._states(store).get("fixture").value)["archive"] == {}


def test_migration_command_requires_confirmation_before_connecting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.swarm import archive_migration

    def refused() -> None:
        pytest.fail("migration connected before writer rollout confirmation")

    monkeypatch.setattr(archive_migration.store, "connect", refused)
    with pytest.raises(SystemExit) as caught:
        archive_migration.main(["fixture"])
    assert caught.value.code == 2
    assert capsys.readouterr().err.endswith("error: upgrade every live fencing writer before migrating the archive\n")


def test_migration_command_runs_the_explicit_migration(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import archive_migration, fencing

    monkeypatch.setattr(archive_migration.store, "connect", lambda: store)
    fencing.write(store, "fixture", lambda doc: doc["archive"].update({"old": {"name": "old"}}))
    archive_migration.main(["fixture", "--writers-upgraded"])
    assert fencing.read(store, "fixture")["archive"] == {"old": {"name": "old"}}
    assert json.loads(fencing._states(store).get("fixture").value)["archive"] == {}


def test_repeating_migration_does_not_write_more_chunks(store: Any) -> None:
    from scripts.swarm import fencing

    fencing.write(store, "fixture", lambda doc: doc["archive"].update({"old": {"name": "old"}}))
    fencing.migrate_archive(store, "fixture", writers_upgraded=True)
    states = fencing._states(store)
    before = states.get("fixture")
    assert json.loads(before.value)["archive_ref"].startswith("fixture:archive:")
    fencing.migrate_archive(store, "fixture", writers_upgraded=True)
    assert states.get("fixture") == before


@pytest.mark.parametrize("corrupt", [False, True])
def test_failed_chunk_persistence_never_removes_inline_history(
    store: Any, monkeypatch: pytest.MonkeyPatch, corrupt: bool
) -> None:
    from scripts.events.bus import StateConflict
    from scripts.events.redis_state import RedisStateStore
    from scripts.swarm import fencing

    fencing.write(store, "fixture", lambda doc: doc["archive"].update({"old": {"name": "old"}}))
    before = fencing.read(store, "fixture")
    create = RedisStateStore.create

    def refused(self: Any, key: str, value: bytes) -> int:
        if ":archive:" in key:
            if corrupt:
                create(self, key, b"corrupt")
            raise StateConflict("archive persistence refused")
        return create(self, key, value)

    monkeypatch.setattr(RedisStateStore, "create", refused)
    with pytest.raises(StateConflict, match="archive persistence refused"):
        fencing.migrate_archive(store, "fixture", writers_upgraded=True)
    assert fencing.read(store, "fixture") == before


def test_migration_module_help_is_safe_before_connecting(capsys: pytest.CaptureFixture[str]) -> None:
    from scripts.swarm import archive_migration

    with pytest.raises(SystemExit) as caught:
        archive_migration.main(["--help"])
    assert caught.value.code == 0
    output = capsys.readouterr().out
    assert "\nMigrate execution history after the fencing reader rollout\n" in output
    assert "--writers-upgraded" in output
    assert "slug" in output


def test_registry_history_callback_preserves_chunk_namespace(store: Any) -> None:
    from scripts.swarm import execution, fencing

    fencing.migrate_archive(store, "fixture", writers_upgraded=True)
    registry = execution.ExecutionRegistry(store)
    registry._fenced("fixture", lambda doc: doc["archive"].update({"old": {"name": "old"}}), history=True)
    raw = json.loads(fencing._states(store).get("fixture").value)
    assert raw["archive"] == {}
    assert raw["archive_ref"].startswith("fixture:archive:")
    assert fencing.read(store, "fixture")["archive"] == {"old": {"name": "old"}}


def test_external_archive_removal_keeps_other_rows(store: Any) -> None:
    from scripts.swarm import fencing

    rows = {"old": {"name": "old"}, "keep": {"name": "keep"}}
    reference = fencing._save_archive_chunk(store, "fixture", "", rows, [])
    doc = {"archive_ref": reference, "archive": {"keep": rows["keep"]}}
    fencing._externalize_archive(store, "fixture", doc)
    raw = json.loads(fencing._states(store).get(doc["archive_ref"]).value)
    assert raw == {"parent": reference, "rows": {}, "removed": ["old"]}
    assert doc["archive"] == {}
    assert fencing._archive_rows(store, doc["archive_ref"]) == {"keep": rows["keep"]}


def test_fenced_execution_readers_find_externalized_history(store: Any) -> None:
    from scripts.swarm import fencing
    from scripts.swarm.store import SwarmError
    from scripts.swarm_v2.authority import TaskAuthority
    from scripts.swarm_v2.runtime.operations import OperationConflict, OperationJournal
    from tests.swarm.test_execution_archive import history

    first, _, _, _ = history(store)
    store.execution_registry.archive("fixture")
    fencing.migrate_archive(store, "fixture", writers_upgraded=True)
    expected = store.execution("fixture", first.execution_id)
    authority = TaskAuthority(store, SimpleNamespace(slug="fixture"), None)  # pyright: ignore[reportArgumentType]
    journal = OperationJournal(store)
    seen: dict[str, Any] = {}

    def check(doc: dict[str, Any]) -> None:
        assert doc["archive"] == {}
        seen["record"] = authority._execution(doc, first.execution_id)
        with pytest.raises(OperationConflict) as caught:
            journal._current(first.execution_id, first.generation, doc)
        seen["error"] = str(caught.value)
        with pytest.raises(SwarmError) as unknown:
            authority._execution(doc, "exe-missing")
        seen["unknown"] = str(unknown.value)

    fencing.write(store, "fixture", check)
    assert seen == {
        "record": expected,
        "error": "execution generation is stale",
        "unknown": "unknown execution identity; display labels cannot identify attempts",
    }


def test_execution_row_prefers_current_then_inline_then_external(store: Any) -> None:
    from scripts.swarm import fencing

    fencing.write(store, "fixture", lambda doc: doc["archive"].update({"old": {"name": "external"}}))
    fencing.migrate_archive(store, "fixture", writers_upgraded=True)
    doc = fencing._decode(fencing._states(store).get("fixture").value)
    assert fencing.execution_row(store, doc, "old") == {"name": "external"}
    doc["archive"]["old"] = {"name": "inline"}
    assert fencing.execution_row(store, doc, "old") == {"name": "inline"}
    doc["executions"]["old"] = {"name": "current"}
    assert fencing.execution_row(store, doc, "old") == {"name": "current"}
    assert fencing.execution_row(store, doc, "absent") is None


def test_execution_row_without_external_history_reads_only_the_document(store: Any) -> None:
    from scripts.swarm import fencing

    assert fencing.execution_row(store, {"executions": {}, "archive": {}}, "absent") is None
