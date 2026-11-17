import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))

from scripts.swarm_ledger.repository import bin_storage
from scripts.swarm_ledger.storage import default as default_repository

repository = default_repository()
IN_PROCESS = "in process bin lock"


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
    from scripts.swarm_ledger.storage import default as default_repository

    repository = default_repository()

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


def _locks_taken(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    taken: list[str] = []
    locked = bin_storage.request_metrics.locked

    def recorded(statement: str) -> None:
        if statement == bin_storage.BEGIN_IMMEDIATE:
            taken.append(statement)

    @contextmanager
    def held(lock: Any) -> Iterator[None]:
        taken.append(IN_PROCESS)
        with locked(lock):
            yield

    monkeypatch.setattr(repository, "trace", recorded)
    monkeypatch.setattr(bin_storage.request_metrics, "locked", held)
    return taken


def _small(slug: str, updated_at: int) -> dict[str, Any]:
    return {
        "slug": slug,
        "size": "small",
        "finished": False,
        "updated_at": updated_at,
        "created_at": updated_at,
        "closed_at": None,
    }


@pytest.mark.feature("ledger-bin-tidy-lock")
def test_an_idle_tidy_pass_takes_no_exclusive_writer_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 10 * bin_storage.domain.DAY_MS
    bin_storage.delete("recent", now=now - bin_storage.domain.DAY_MS)
    monkeypatch.setattr(repository, "summaries", lambda connection: [_small("active", now)])
    taken = _locks_taken(monkeypatch)
    assert bin_storage.domain.tidy(now) == ([], [])
    assert taken == []
    assert bin_storage.entries() == {"recent": now - bin_storage.domain.DAY_MS}


@pytest.mark.feature("ledger-bin-tidy-lock")
def test_a_bin_entry_at_exactly_the_kept_days_is_kept_without_the_writer_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    kept = bin_storage.domain.KEEP_DAYS * bin_storage.domain.DAY_MS
    bin_storage.delete("edge", now=5)
    taken = _locks_taken(monkeypatch)
    assert bin_storage.purge_expired(now=5 + kept) == []
    assert taken == []
    assert bin_storage.entries() == {"edge": 5}


@pytest.mark.feature("ledger-bin-tidy-lock")
def test_an_expired_bin_entry_is_purged_under_the_writer_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    kept = bin_storage.domain.KEEP_DAYS * bin_storage.domain.DAY_MS
    bin_storage.delete("gone", now=5)
    bin_storage.delete("young", now=5 + kept)
    taken = _locks_taken(monkeypatch)
    assert bin_storage.purge_expired(now=6 + kept) == ["gone"]
    assert taken == [IN_PROCESS, bin_storage.BEGIN_IMMEDIATE]
    assert bin_storage.entries() == {"young": 5 + kept}


@pytest.mark.feature("ledger-bin-tidy-lock")
def test_a_ledger_due_for_the_bin_is_binned_under_the_writer_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 100 * bin_storage.domain.DAY_MS
    bin_storage.delete("parked", now=now)
    monkeypatch.setattr(
        repository, "summaries", lambda connection: [_small("stale", 0), _small("parked", 0), _small("fresh", now)]
    )
    taken = _locks_taken(monkeypatch)
    assert bin_storage.auto_bin(now=now) == ["stale"]
    assert taken == [IN_PROCESS, bin_storage.BEGIN_IMMEDIATE]
    assert bin_storage.entries() == {"parked": now, "stale": now}


@pytest.mark.feature("ledger-bin-tidy-lock")
def test_a_recent_restore_keeps_an_old_ledger_out_without_the_writer_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 100 * bin_storage.domain.DAY_MS
    bin_storage.delete("back", now=1)
    bin_storage.restore("back", now=now - bin_storage.domain.DAY_MS)
    monkeypatch.setattr(repository, "summaries", lambda connection: [_small("back", 0)])
    taken = _locks_taken(monkeypatch)
    assert bin_storage.auto_bin(now=now) == []
    assert taken == []
    assert bin_storage.entries() == {}


@pytest.mark.feature("ledger-bin-tidy-lock")
def test_a_bin_entry_without_a_time_is_never_purged(monkeypatch: pytest.MonkeyPatch) -> None:
    store: Any = repository
    with store.connect() as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        store.save_registry(connection, "bin", {"odd": "later"})
    taken = _locks_taken(monkeypatch)
    assert bin_storage.purge_expired(now=100 * bin_storage.domain.DAY_MS) == []
    assert taken == []
    assert bin_storage.registries()["bin"] == {"odd": "later"}
