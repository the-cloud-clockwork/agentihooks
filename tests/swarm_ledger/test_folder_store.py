import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import outbox_host, storage
from scripts.swarm_ledger.repository import bin_storage, postgres, sqlite
from tests.swarm_ledger.test_sqlite import CONTENT, store

API_URL = "AGENTIHOOKS_SWARM_API_URL"
DATABASE_URL = "postgresql://ledger@db/ledger"


@pytest.mark.parametrize(
    ("database", "folder", "built"),
    [
        (None, False, sqlite.SQLiteLedgerRepository),
        (DATABASE_URL, False, postgres.PostgresLedgerRepository),
        (DATABASE_URL, True, sqlite.SQLiteLedgerRepository),
    ],
)
def test_storage_builds_the_folder_or_the_shared_database_and_never_reads_an_api_address(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, database: str | None, folder: bool, built: type
) -> None:
    monkeypatch.setenv(API_URL, "http://api:8770")
    if database is None:
        monkeypatch.delenv(postgres.URL, raising=False)
    else:
        monkeypatch.setenv(postgres.URL, database)

    assert type(storage.build(tmp_path / storage.DATABASE if folder else None)) is built


@pytest.mark.parametrize("database", [DATABASE_URL, None])
def test_the_shared_address_is_the_database_url_whatever_the_api_address(
    monkeypatch: pytest.MonkeyPatch, database: str | None
) -> None:
    monkeypatch.setenv(API_URL, "http://api:8770")
    if database is None:
        monkeypatch.delenv(postgres.URL, raising=False)
    else:
        monkeypatch.setenv(postgres.URL, database)

    assert storage.shared() == database


def written(tmp_path: Path, change: Callable[[dict], None]) -> tuple[sqlite.SQLiteLedgerRepository, dict]:
    """Create the ledger, write its stored state changed by change at the next revision, and return the repository
    with the state the write stored."""
    repository = store(tmp_path)
    repository.create("ledger", CONTENT)
    with repository.connect() as connection:
        entry = repository._entry(connection, "ledger")
    state = json.loads(entry.text)
    change(state)
    state["_meta"]["rev"] += 1
    with repository.connect() as connection, connection:
        after = repository._write(connection, "ledger", entry, state, []).state
    return repository, after


def renamed(state: dict) -> None:
    state["title"] = "Renamed"


def test_a_write_stores_the_summary_of_the_state_it_wrote(tmp_path: Path) -> None:
    repository, _ = written(tmp_path, renamed)

    assert [(summary["slug"], summary["title"]) for summary in repository.list_summaries()] == [("ledger", "Renamed")]


def test_task_rows_carry_the_stored_task_revision_and_revision(tmp_path: Path) -> None:
    repository, after = written(tmp_path, renamed)

    _, meta, _ = repository.task_rows("ledger")

    assert meta == {"task_revision": after["_meta"]["task_revision"], "rev": after["_meta"]["rev"]}


def folder_with_bin(path: Path, entries: dict) -> sqlite.SQLiteLedgerRepository:
    """A ledger folder whose bin registry holds entries."""
    repository = sqlite.SQLiteLedgerRepository(path / sqlite.DATABASE)
    with repository.connect() as connection, connection:
        repository.save_registry(connection, "bin", entries)
    return repository


def test_bin_entries_are_the_integer_stamps_of_the_folder_bin_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = folder_with_bin(tmp_path, {"split": 5, "note": "kept"})
    monkeypatch.setattr(storage, "default", lambda: folder)

    assert bin_storage.entries() == {"split": 5}


def test_the_bin_reads_the_folder_of_whichever_repository_is_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = folder_with_bin(tmp_path / "first", {"one": 1})
    second = folder_with_bin(tmp_path / "second", {"two": 2})
    read = []
    for folder in (second, first):
        monkeypatch.setattr(storage, "default", lambda folder=folder: folder)
        read.append(bin_storage.entries())

    assert read == [{"two": 2}, {"one": 1}]


def test_the_outbox_host_runs_a_relay_for_a_repository_flagged_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    built, closed = [], []

    class Interval:
        def __init__(self, publish: Callable[[], None], every: float) -> None:
            self.publish, self.every = publish, every
            built.append(every)

        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setenv("AGENTIHOOKS_BUS", "nats")
    monkeypatch.setattr(outbox_host, "Interval", Interval)
    monkeypatch.setattr(outbox_host.Relay, "close", lambda relay: closed.append(relay.repository))
    repository = SimpleNamespace(remote=True)

    with outbox_host.running(repository):
        pass

    assert (built, closed) == ([outbox_host.INTERVAL], [repository])


def served(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[list[dict], list[float]]:
    """Run the ledger server's startup on the folder backend with an API address set, and return the threads it
    started and the waits of every writer lease it took."""
    from scripts.swarm_ledger import ledger_server as server

    threads, waits = [], []

    class Thread:
        def __init__(self, **kwargs: object) -> None:
            threads.append(kwargs)

        def start(self) -> None:
            pass

    class Server:
        def __init__(self, address: object, handler_class: object, bind_and_activate: bool = True) -> None:
            pass

        def server_bind(self) -> None:
            pass

        def server_activate(self) -> None:
            pass

        def serve_forever(self, poll_interval: float) -> None:
            pass

        def drained(self, timeout: float) -> bool:
            return True

        def server_close(self) -> None:
            pass

    class Lease:
        def __init__(self, directory: object) -> None:
            pass

        def acquire(self, claimant: object, wait: float) -> "Lease":
            waits.append(wait)
            return self

        def release(self) -> None:
            pass

    monkeypatch.setenv(API_URL, "http://api:8770")
    monkeypatch.setattr(server.threading, "Thread", Thread)
    monkeypatch.setattr(server, "ThreadingHTTPServer", Server)
    monkeypatch.setattr(server, "HUB", SimpleNamespace(drain=lambda: None))
    monkeypatch.setattr(server.server_lifetime, "watch", lambda *args: server.threading.Event())
    monkeypatch.setattr(server.ports.swarm().ledger_writer, "WriterLease", Lease)
    monkeypatch.setattr(server.legacy, "adopt", lambda repository: None)
    monkeypatch.setattr(server, "PIDFILE", tmp_path / ".server.pid")
    monkeypatch.setattr(server.storage, "shared", lambda: None)
    server.serve()
    return threads, waits


def test_the_ledger_server_takes_the_folder_writer_lease_with_an_api_address_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, waits = served(monkeypatch, tmp_path)

    assert waits == [0.0]


def test_the_ledger_server_follows_no_shared_database_with_an_api_address_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from scripts.swarm_ledger import ledger_server as server

    threads, _ = served(monkeypatch, tmp_path)

    assert server.follow_shared not in [getattr(thread["target"], "__wrapped__", None) for thread in threads]
