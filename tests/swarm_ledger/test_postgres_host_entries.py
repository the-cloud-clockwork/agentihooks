import sqlite3
import threading
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from scripts.swarm_ledger.repository import host_entries, postgres, sqlite

STATE = {"title": "copy", "tasks": [], "_meta": {"rev": 1, "events": []}}


@pytest.fixture
def connected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Any, Any, Any]:
    raw = Mock(status=0)

    def execute(sql: str, parameters: object = ()) -> None:
        raw.status = 2

    def commit() -> None:
        raw.status = 0

    raw.get_transaction_status.side_effect = lambda: raw.status
    raw.cursor.return_value.execute.side_effect = execute
    raw.cursor.return_value.fetchone.return_value = (100,)
    raw.commit.side_effect = commit
    raw.rollback.side_effect = commit
    pool = Mock()
    pool.take.return_value = raw
    monkeypatch.setattr(postgres, "pool", lambda address: pool)
    monkeypatch.setattr(sqlite, "load", lambda connection, slug: deepcopy(STATE))
    monkeypatch.setattr(sqlite, "read_events", lambda connection, slug: [])
    copies = host_entries.Copies(tmp_path / "entries", "database")
    repository = postgres.PostgresLedgerRepository("database", copies=copies)
    return repository, postgres.Connection("database"), raw


@pytest.mark.feature("ledger-copy-after-commit")
@pytest.mark.parametrize("begin", [sqlite.BEGIN, sqlite.BEGIN_IMMEDIATE], ids=["read", "write"])
def test_a_slow_host_copy_observes_a_committed_postgres_transaction(
    begin: str, connected: tuple[Any, Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, connection, raw = connected
    started, release = threading.Event(), threading.Event()
    observed = []
    keep = repository.copies.keep

    def held_copy(slug: str, entry: Any, whole: bool) -> None:
        started.set()
        assert release.wait(10)
        keep(slug, entry, whole)

    def observe() -> None:
        if started.wait(10):
            observed.append((raw.status, raw.commit.call_count))
        release.set()

    monkeypatch.setattr(repository.copies, "keep", held_copy)
    observer = threading.Thread(target=observe)
    observer.start()
    try:
        with connection:
            connection.execute(begin)
            repository._entry(connection, "copy")
    finally:
        release.set()
        observer.join(10)
    assert not observer.is_alive()
    assert observed == [(0, 1)]
    assert host_entries.read(host_entries.path(repository.copies.held, ("database", "copy")), 100)[1].state == STATE


@pytest.mark.feature("ledger-copy-after-commit")
def test_a_rolled_back_entry_is_never_written_to_the_host(connected: tuple[Any, Any, Any]) -> None:
    repository, connection, raw = connected
    with pytest.raises(RuntimeError, match="rollback"):
        with connection:
            connection.execute(sqlite.BEGIN)
            repository._entry(connection, "copy")
            raise RuntimeError("rollback")
    connection.commit()
    assert raw.rollback.call_count == 1
    assert not repository.copies.held.exists()


@pytest.mark.feature("ledger-copy-after-commit")
def test_a_failed_commit_discards_the_pending_host_copy(connected: tuple[Any, Any, Any]) -> None:
    repository, connection, raw = connected
    raw.commit.side_effect = sqlite3.OperationalError("commit refused")
    with pytest.raises(sqlite3.OperationalError, match="commit refused"), connection:
        connection.execute(sqlite.BEGIN)
        repository._entry(connection, "copy")
    raw.commit.side_effect = None
    connection.commit()
    assert not repository.copies.held.exists()


@pytest.mark.feature("ledger-copy-after-commit")
def test_closing_without_commit_discards_the_pending_host_copy(connected: tuple[Any, Any, Any]) -> None:
    repository, connection, _ = connected
    connection.execute(sqlite.BEGIN)
    repository._entry(connection, "copy")
    connection.close()
    assert not repository.copies.held.exists()


@pytest.mark.feature("ledger-copy-after-commit")
def test_a_purge_drops_the_copy_after_its_queued_save(
    connected: tuple[Any, Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, connection, raw = connected
    monkeypatch.setattr(sqlite.outbox, "write", lambda *args: None)
    drop = repository.copies.drop
    observed = []

    def dropping(slug: str) -> None:
        observed.append((raw.status, raw.commit.call_count))
        drop(slug)

    monkeypatch.setattr(repository.copies, "drop", dropping)
    with connection:
        connection.execute(sqlite.BEGIN_IMMEDIATE)
        repository.purge("copy", connection)
    assert observed == [(0, 1)]
    assert not host_entries.path(repository.copies.held, ("database", "copy")).exists()


@pytest.mark.feature("ledger-copy-after-commit")
def test_a_deferred_older_copy_never_replaces_a_newer_generation(tmp_path: Path) -> None:
    copies = host_entries.Copies(tmp_path / "entries", "database")
    copies.keep("copy", SimpleNamespace(generation=200, text='{"new":true}'), True)
    copies.keep("copy", SimpleNamespace(generation=100, text='{"old":true}'), True)
    assert copies.generations == {"copy": 200}
    assert host_entries.read(host_entries.path(copies.held, ("database", "copy")), 200)[1].state == {"new": True}
