"""A ledger whose events no stream takes never holds back another ledger's outbox rows."""

from pathlib import Path

import pytest

from scripts.events import memory
from scripts.events.bus import NoStream
from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.swarm_ledger.repository import outbox, sqlite
from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository

CONTENT = {
    "title": "Store",
    "overview": "o",
    "sources": [],
    "phases": [{"title": "One", "description": "d"}],
    "tasks": [{"title": "first", "phase": "p1", "lane": "eng"}],
}
CLAIM = {"op": "task_update", "id": "u1", "by": "eng", "item": "tasks/t1", "fields": {"state": "claimed"}}


class Unclaimed:
    def __init__(self, events: memory.MemoryEventLog, refused: str) -> None:
        self.events, self.refused = events, refused

    def publish(self, subject: str, event: Envelope) -> bool:
        if self.refused in (event.swarm, subject):
            raise NoStream(subject)
        return self.events.publish(subject, event)


@pytest.fixture
def repo(tmp_path: Path) -> SQLiteLedgerRepository:
    repository = SQLiteLedgerRepository(tmp_path / "ledgers" / sqlite.DATABASE)
    repository.create("ledger", CONTENT, "swarm")
    repository.create("alpha", CONTENT, "swarm")
    return repository


@pytest.fixture
def log() -> memory.MemoryEventLog:
    log = memory.open_bus(BusConfig(), clock=lambda: 1_000).events
    assert isinstance(log, memory.MemoryEventLog)
    return log


def rows(repo: SQLiteLedgerRepository, slug: str) -> list[tuple[str, str]]:
    with repo.connect() as connection:
        found = connection.execute(
            "SELECT id, subject FROM outbox WHERE slug=? ORDER BY revision, subject", (slug,)
        ).fetchall()
    return [(row_id, subject) for row_id, subject in found]


def sent(repo: SQLiteLedgerRepository) -> list[str]:
    with repo.connect() as connection:
        return sorted(row[0] for row in connection.execute("SELECT id FROM outbox_sent").fetchall())


def published(log: memory.MemoryEventLog) -> list[tuple[str, str]]:
    return [(event.id, subject) for subject, event in log.entries]


@pytest.mark.feature("ledger-event-relay")
def test_a_ledger_whose_events_no_stream_takes_never_holds_back_another_ledgers_rows(
    repo: SQLiteLedgerRepository, log: memory.MemoryEventLog
) -> None:
    held, taken = rows(repo, "alpha"), rows(repo, "ledger")
    assert outbox.relay(repo, Unclaimed(log, "alpha")) == 2
    assert published(log) == taken
    assert sent(repo) == sorted(row_id for row_id, _ in taken)
    assert outbox.relay(repo, log) == 2
    assert published(log) == taken + held
    assert sent(repo) == sorted(row_id for row_id, _ in held + taken)


@pytest.mark.feature("ledger-event-relay")
def test_a_row_no_stream_takes_keeps_the_later_rows_of_its_ledger_unsent_in_order(
    repo: SQLiteLedgerRepository, log: memory.MemoryEventLog
) -> None:
    repo.apply_ops("ledger", ops=[CLAIM])
    first, refused, *later = rows(repo, "ledger")
    alpha = rows(repo, "alpha")
    assert outbox.relay(repo, Unclaimed(log, refused[1])) == 1 + len(alpha)
    assert published(log) == alpha + [first]
    assert outbox.relay(repo, log) == 1 + len(later)
    assert published(log) == alpha + [first, refused, *later]


@pytest.mark.feature("ledger-event-relay")
def test_each_ledger_takes_its_own_batch_of_rows(repo: SQLiteLedgerRepository, log: memory.MemoryEventLog) -> None:
    alpha, ledger = rows(repo, "alpha"), rows(repo, "ledger")
    assert outbox.relay(repo, log, batch=1) == 2
    assert published(log) == [alpha[0], ledger[0]]


@pytest.mark.feature("ledger-outbox-small-ledgers")
def test_a_relay_beside_a_small_ledger_publishes_the_swarm_ledgers_rows_and_leaves_no_unsent_row(
    tmp_path: Path, log: memory.MemoryEventLog
) -> None:
    repository = SQLiteLedgerRepository(tmp_path / "ledgers" / sqlite.DATABASE)
    repository.create("session", CONTENT)
    repository.create("crew", CONTENT, "swarm")
    with repository.transaction() as transaction:
        transaction.purge("session")
    taken = rows(repository, "crew")
    assert rows(repository, "session") == []
    assert outbox.relay(repository, Unclaimed(log, "session")) == len(taken) == 2
    assert published(log) == taken
    with repository.connect() as connection:
        assert connection.execute(outbox.SLUGS).fetchall() == []
