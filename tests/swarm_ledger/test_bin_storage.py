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


def test_a_transaction_holds_the_write_lock_on_the_shared_repository():
    from scripts.swarm_ledger.repository import repository

    with bin_storage._transaction() as (held, connection):
        assert held is repository
        assert connection.in_transaction
        assert bin_storage.domain.LOCK.locked()
    assert not bin_storage.domain.LOCK.locked()


def test_restore_merges_into_the_existing_restored_registry():
    bin_storage.delete("a", now=1)
    bin_storage.restore("a", now=10)
    bin_storage.delete("b", now=2)
    bin_storage.restore("b", now=20)
    assert bin_storage.restored() == {"a": 10, "b": 20}


def test_restored_reads_only_the_restored_registry_ints():
    bin_storage.delete("other", now=1)
    bin_storage.restore("other", now=2)
    assert bin_storage.restored() == {"other": 2}
    assert bin_storage.entries() == {}


def test_registries_returns_both_the_bin_and_restored_registries_under_their_names():
    bin_storage.delete("p", now=5)
    bin_storage.delete("q", now=6)
    bin_storage.restore("q", now=7)
    assert bin_storage.registries() == {"bin": {"p": 5}, "restored": {"q": 7}}


def test_auto_bin_does_not_fall_back_to_zero_when_created_at_is_set(monkeypatch):
    monkeypatch.setattr(
        repository,
        "summaries",
        lambda connection: (
            connection.in_transaction
            and [
                {
                    "slug": "x",
                    "size": "small",
                    "finished": False,
                    "updated_at": None,
                    "created_at": 1000,
                    "closed_at": None,
                }
            ]
        ),
    )
    assert bin_storage.auto_bin(now=604800500) == []


def test_auto_bin_treats_a_never_touched_ledger_as_idle_from_time_zero(monkeypatch):
    monkeypatch.setattr(
        repository,
        "summaries",
        lambda connection: (
            connection.in_transaction
            and [
                {
                    "slug": "y",
                    "size": "small",
                    "finished": False,
                    "updated_at": None,
                    "created_at": None,
                    "closed_at": None,
                }
            ]
        ),
    )
    assert bin_storage.auto_bin(now=604800001) == ["y"]
