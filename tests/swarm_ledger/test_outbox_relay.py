import pytest

from scripts.events import memory
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


class Crash(Exception):
    pass


class CrashAfterPublish:
    def __init__(self, events):
        self.events = events

    def publish(self, subject, event):
        self.events.publish(subject, event)
        raise Crash("crashed before the sent mark")


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
        return connection.execute("SELECT id,subject,value FROM outbox ORDER BY revision,subject").fetchall()


def sent(repo):
    with repo.connect() as connection:
        return sorted(row[0] for row in connection.execute("SELECT id FROM outbox_sent").fetchall())


def test_the_relay_publishes_each_unsent_row_on_its_subject_with_the_row_id_and_marks_it_sent(repo, bus):
    created = rows(repo)
    assert outbox.relay(repo, bus.events) == 2
    assert [(subject, event) for subject, event in bus.events.entries] == [
        (subject, Envelope.decode(value.encode())) for _, subject, value in created
    ]
    assert [event.id for _, event in bus.events.entries] == [row_id for row_id, *_ in created]
    assert sent(repo) == sorted(row_id for row_id, *_ in created)


def test_a_sent_row_is_never_published_again(repo, bus):
    outbox.relay(repo, bus.events)
    assert outbox.relay(repo, bus.events) == 0
    repo.apply_ops("ledger", ops=[CLAIM])
    [claimed] = [row for row in rows(repo) if row[0] not in sent(repo)]
    assert outbox.relay(repo, bus.events) == 1
    assert [(subject, event.id) for subject, event in bus.events.entries[2:]] == [(claimed[1], claimed[0])]


def test_the_relay_takes_at_most_one_batch_of_rows_in_revision_order(repo, bus):
    repo.apply_ops("ledger", ops=[CLAIM])
    ordered = [row_id for row_id, *_ in rows(repo)]
    assert outbox.relay(repo, bus.events, batch=1) == 1
    assert outbox.relay(repo, bus.events, batch=2) == 2
    assert outbox.relay(repo, bus.events, batch=2) == 0
    assert [event.id for _, event in bus.events.entries] == ordered


def test_the_default_batch_is_one_hundred_rows(tmp_path, bus):
    repository = SQLiteLedgerRepository(tmp_path / "ledgers" / sqlite.DATABASE)
    tasks = [{"title": f"task {number}", "phase": "p1", "lane": "eng"} for number in range(101)]
    repository.create("ledger", {**CONTENT, "tasks": tasks})
    assert len(rows(repository)) == 102
    assert outbox.relay(repository, bus.events) == 100
    assert outbox.relay(repository, bus.events) == 2


def test_a_crash_between_publish_and_mark_never_duplicates_the_event(repo, bus):
    first = rows(repo)[0]
    with pytest.raises(Crash) as caught:
        outbox.relay(repo, CrashAfterPublish(bus.events))
    assert str(caught.value) == "crashed before the sent mark"
    assert sent(repo) == []
    assert outbox.relay(repo, bus.events) == 2
    assert [event.id for _, event in bus.events.entries] == [first[0], rows(repo)[1][0]]
    assert sent(repo) == sorted(row_id for row_id, *_ in rows(repo))


def test_the_sent_table_is_part_of_the_ledger_schema():
    assert "CREATE TABLE IF NOT EXISTS outbox_sent (id TEXT PRIMARY KEY);" in sqlite.SCHEMA
