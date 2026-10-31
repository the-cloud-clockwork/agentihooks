import json
from typing import Any

import pytest

pytestmark = pytest.mark.xdist_group("fakeredis")


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
    fencing.write(store, "fixture", lambda doc: doc["counters"].update({"sample": 1}))
    assert fencing.read(store, "fixture")["archive"] == rows
    assert fencing.read(store, "fixture")["counters"]["sample"] == 1


def test_migration_requires_the_reader_rollout(store: Any) -> None:
    from scripts.swarm import fencing
    from scripts.swarm.store import SwarmError

    fencing.write(store, "fixture", lambda doc: doc["archive"].update({"old": {"name": "old"}}))
    before = fencing._states(store).get("fixture")
    with pytest.raises(SwarmError, match="upgrade every live fencing writer"):
        fencing.migrate_archive(store, "fixture")
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
    fencing.write(store, "fixture", lambda doc: doc["counters"].update({"sample": 2}))
    assert json.loads(fencing._states(store).get("fixture").value)["counters"]["sample"] == 2


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
    fencing.write(store, "fixture", lambda doc: doc["counters"].update({"sample": 3}))
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
    assert store.execution("fixture", second.execution_id) == second
    registry.set_pane("fixture", first.execution_id, "old-pane")
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


def test_empty_archive_and_snapshots_round_trip(store: Any) -> None:
    from scripts.swarm import fencing

    fencing.migrate_archive(store, "fixture", writers_upgraded=True)
    empty = fencing.read(store, "fixture")
    assert empty["archive"] == {}
    fencing.change_with_archive(store, "fixture", empty, lambda doc: doc["archive"].update({"old": {"name": "old"}}))
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

    fencing.write(store, "fixture", lambda doc: doc.update(archive_ref="missing"))
    with pytest.raises(SwarmError, match="referenced execution archive is unavailable"):
        fencing.read(store, "fixture")
