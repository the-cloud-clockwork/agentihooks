import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import outbox_host, storage
from scripts.swarm_ledger.repository import bin_storage, postgres, sqlite
from tests.swarm_ledger.test_sqlite import CONTENT, store

API_URL = "AGENTIHOOKS_SWARM_API_URL"


@pytest.mark.parametrize(
    ("api", "database", "built"),
    [
        ("http://api:8770", None, sqlite.SQLiteLedgerRepository),
        ("http://api:8770", "postgresql://ledger@db/ledger", postgres.PostgresLedgerRepository),
        (None, None, sqlite.SQLiteLedgerRepository),
    ],
)
def test_storage_builds_the_folder_or_the_shared_database_and_never_reads_an_api_address(
    monkeypatch: pytest.MonkeyPatch, api: str | None, database: str | None, built: type
) -> None:
    for name, value in ((API_URL, api), (postgres.URL, database)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    assert type(storage.build()) is built


def written(tmp_path: Path, change: Callable[[dict], None]) -> tuple[sqlite.SQLiteLedgerRepository, dict, dict]:
    """Create the ledger, write its stored state changed by change at the next revision, and return the repository
    with the state before and after the write."""
    repository = store(tmp_path)
    repository.create("ledger", CONTENT)
    with repository.connect() as connection:
        entry = repository._entry(connection, "ledger")
    before = json.loads(entry.text)
    state = json.loads(entry.text)
    change(state)
    state["_meta"]["rev"] = before["_meta"]["rev"] + 1
    with repository.connect() as connection, connection:
        after = repository._write(connection, "ledger", entry, state, []).state
    return repository, before, after


def renamed(state: dict) -> None:
    state["title"] = "Renamed"


def first_task_done(state: dict) -> None:
    state["tasks"] = [{**task, "done": index == 0} for index, task in enumerate(state["tasks"])]


def test_a_write_stores_the_summary_of_the_state_it_wrote(tmp_path: Path) -> None:
    repository, _, _ = written(tmp_path, renamed)

    assert [(summary["slug"], summary["title"]) for summary in repository.list_summaries()] == [("ledger", "Renamed")]


def test_a_write_counts_the_open_and_done_tasks_of_the_state_it_wrote(tmp_path: Path) -> None:
    repository, _, _ = written(tmp_path, first_task_done)

    assert [(summary["open"], summary["done"]) for summary in repository.list_summaries()] == [(1, 1)]


def test_a_write_that_changes_the_tasks_moves_the_task_revision_to_the_written_revision(tmp_path: Path) -> None:
    _, before, after = written(tmp_path, first_task_done)

    assert after["_meta"]["task_revision"] == before["_meta"]["rev"] + 1


def test_a_write_that_keeps_the_tasks_keeps_the_task_revision_it_had(tmp_path: Path) -> None:
    _, before, after = written(tmp_path, renamed)

    assert after["_meta"]["task_revision"] == before["_meta"].get("task_revision", before["_meta"]["rev"])


def test_task_rows_carry_the_stored_task_revision_and_revision(tmp_path: Path) -> None:
    repository, _, after = written(tmp_path, renamed)

    _, meta, _ = repository.task_rows("ledger")

    assert meta == {"task_revision": after["_meta"]["task_revision"], "rev": after["_meta"]["rev"]}


@pytest.fixture
def folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite.SQLiteLedgerRepository:
    repository = sqlite.SQLiteLedgerRepository(tmp_path / sqlite.DATABASE)
    monkeypatch.setattr(storage, "default", lambda: repository)
    return repository


def test_bin_entries_are_the_integer_stamps_of_the_folder_bin_registry(folder: sqlite.SQLiteLedgerRepository) -> None:
    with folder.connect() as connection, connection:
        folder.save_registry(connection, "bin", {"split": 5, "note": "kept"})

    assert bin_storage.entries() == {"split": 5}


def test_bin_registries_are_read_from_the_folder_of_the_default_repository(
    folder: sqlite.SQLiteLedgerRepository,
) -> None:
    with folder.connect() as connection, connection:
        folder.save_registry(connection, "restored", {"back": 9})

    assert bin_storage.registries() == {"bin": {}, "restored": {"back": 9}}


def test_the_outbox_host_relays_any_repository_when_the_bus_is_not_in_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    closed = []

    class Interval:
        def __init__(self, publish: Callable[[], None], every: float) -> None:
            self.publish, self.every = publish, every

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

    assert closed == [repository]
