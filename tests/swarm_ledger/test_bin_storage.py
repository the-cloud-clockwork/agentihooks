import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))

from scripts.swarm_ledger.repository import bin_storage, repository


@pytest.fixture(autouse=True)
def empty_bin():
    with repository.connect() as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        repository.save_registry(connection, "bin", {})
        repository.save_registry(connection, "restored", {})


def test_repository_only_adopts_stray_bin_registries_not_arbitrary_legacy_files(tmp_path):
    (repository.directory / "unrelated.json").write_text("{}", encoding="utf-8")
    bin_storage.entries()
    assert (repository.directory / "unrelated.json").exists()


def test_mark_merges_into_the_existing_registry_not_a_fresh_one():
    bin_storage.delete("first", now=1)
    bin_storage.delete("second", now=2)
    assert bin_storage.entries() == {"first": 1, "second": 2}


def test_mark_passes_the_repository_and_connection_through_to_entries_of():
    seen = {}

    def capture(found, repository, connection):
        seen["repository"] = repository
        seen["connection"] = connection
        return "result"

    result = bin_storage._mark("bin", capture)
    assert result == "result"
    assert seen["repository"] is not None and hasattr(seen["repository"], "registry")
    assert seen["connection"] is not None


def test_restore_merges_into_the_existing_restored_registry():
    bin_storage.delete("a", now=1)
    bin_storage.restore("a", now=10)
    bin_storage.delete("b", now=2)
    bin_storage.restore("b", now=20)
    assert bin_storage.restored() == {"a": 10, "b": 20}


def test_auto_bin_does_not_fall_back_to_zero_when_created_at_is_set(monkeypatch):
    monkeypatch.setattr(
        repository,
        "summaries",
        lambda: [
            {"slug": "x", "size": "small", "finished": False, "updated_at": None, "created_at": 1000, "closed_at": None}
        ],
    )
    assert bin_storage.auto_bin(now=604800500) == []


def test_auto_bin_treats_a_never_touched_ledger_as_idle_from_time_zero(monkeypatch):
    monkeypatch.setattr(
        repository,
        "summaries",
        lambda: [
            {"slug": "y", "size": "small", "finished": False, "updated_at": None, "created_at": None, "closed_at": None}
        ],
    )
    assert bin_storage.auto_bin(now=604800001) == ["y"]
