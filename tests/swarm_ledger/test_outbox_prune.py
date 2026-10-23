import sqlite3
import time

import pytest

from scripts.events import memory
from scripts.events.config import BusConfig
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
WINDOW = 120_000
SENT_AT = 1_000_000


@pytest.fixture
def repo(tmp_path):
    repository = SQLiteLedgerRepository(tmp_path / "ledgers" / sqlite.DATABASE)
    repository.create("ledger", CONTENT)
    return repository


@pytest.fixture
def bus():
    return memory.open_bus(BusConfig(), clock=lambda: 1_000)


def rows(repo):
    with repo.connect() as connection:
        return [row[0] for row in connection.execute("SELECT id FROM outbox ORDER BY revision,subject").fetchall()]


def marks(repo):
    with repo.connect() as connection:
        return connection.execute("SELECT id, sent_at FROM outbox_sent ORDER BY sent_at, id").fetchall()


def test_the_relay_stamps_each_sent_mark_with_the_time_it_was_marked(repo, bus):
    created = rows(repo)
    assert outbox.relay(repo, bus.events, clock=lambda: SENT_AT) == 2
    assert marks(repo) == sorted((row_id, SENT_AT) for row_id in created)


def test_the_relay_stamps_marks_with_the_wall_clock_in_milliseconds_by_default(repo, bus):
    before = time.time_ns() // 1_000_000
    outbox.relay(repo, bus.events)
    after = time.time_ns() // 1_000_000
    assert [before <= sent_at <= after for _, sent_at in marks(repo)] == [True, True]


def test_a_sent_row_and_its_mark_older_than_the_window_are_deleted(repo, bus):
    outbox.relay(repo, bus.events, clock=lambda: SENT_AT)
    assert outbox.prune(repo, WINDOW, clock=lambda: SENT_AT + WINDOW + 1) == 2
    assert rows(repo) == []
    assert marks(repo) == []


def test_an_unsent_row_stays_whatever_its_age(repo, bus):
    outbox.relay(repo, bus.events, clock=lambda: SENT_AT)
    repo.apply_ops("ledger", ops=[CLAIM])
    [unsent] = [row_id for row_id in rows(repo) if row_id not in dict(marks(repo))]
    assert outbox.prune(repo, WINDOW, clock=lambda: SENT_AT + 10 * WINDOW) == 2
    assert rows(repo) == [unsent]
    assert marks(repo) == []
    assert outbox.relay(repo, bus.events) == 1
    assert bus.events.entries[-1][1].id == unsent


def test_a_sent_row_inside_the_window_stays_with_its_mark(repo, bus):
    created = rows(repo)
    outbox.relay(repo, bus.events, clock=lambda: SENT_AT)
    assert outbox.prune(repo, WINDOW, clock=lambda: SENT_AT + WINDOW) == 0
    assert rows(repo) == created
    assert marks(repo) == sorted((row_id, SENT_AT) for row_id in created)
    assert outbox.relay(repo, bus.events) == 0


def test_only_the_rows_marked_before_the_window_are_deleted(repo, bus):
    [first, second] = rows(repo)
    outbox.relay(repo, bus.events, batch=1, clock=lambda: SENT_AT)
    outbox.relay(repo, bus.events, batch=1, clock=lambda: SENT_AT + 5)
    assert outbox.prune(repo, WINDOW, clock=lambda: SENT_AT + WINDOW + 1) == 1
    assert rows(repo) == [second]
    assert marks(repo) == [(second, SENT_AT + 5)]


def test_an_orphan_mark_older_than_the_window_is_deleted(repo, bus):
    outbox.relay(repo, bus.events, clock=lambda: SENT_AT)
    with repo.connect() as connection, connection:
        connection.execute(outbox.SENT, ("gone", SENT_AT))
    assert outbox.prune(repo, WINDOW, clock=lambda: SENT_AT + WINDOW + 1) == 2
    assert marks(repo) == []


def test_the_prune_uses_the_wall_clock_in_milliseconds_by_default(repo, bus):
    now = time.time_ns() // 1_000_000
    outbox.relay(repo, bus.events, clock=lambda: now - WINDOW - 60_000)
    assert outbox.prune(repo, WINDOW) == 2
    assert rows(repo) == []


def test_a_mark_table_without_the_sent_time_gains_it_and_its_old_marks_count_as_sent_at_zero(tmp_path, bus):
    path = tmp_path / "ledgers" / sqlite.DATABASE
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as legacy:
        legacy.execute("CREATE TABLE outbox_sent (id TEXT PRIMARY KEY)")
        legacy.execute("INSERT INTO outbox_sent VALUES ('old')")
    repository = SQLiteLedgerRepository(path)
    repository.create("ledger", CONTENT)
    assert marks(repository) == [("old", 0)]
    created = rows(repository)
    outbox.relay(repository, bus.events, clock=lambda: SENT_AT)
    assert outbox.prune(repository, WINDOW, clock=lambda: WINDOW + 1) == 0
    assert marks(repository) == sorted((row_id, SENT_AT) for row_id in created)
