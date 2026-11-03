import pytest

from scripts.events import memory
from scripts.events.bus import PayloadTooLarge
from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope, create
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


@pytest.mark.feature("ledger-event-relay")
def test_the_relay_publishes_each_unsent_row_on_its_subject_with_the_row_id_and_marks_it_sent(repo, bus):
    created = rows(repo)
    assert outbox.relay(repo, bus.events) == 2
    assert [(subject, event) for subject, event in bus.events.entries] == [
        (subject, Envelope.decode(value.encode())) for _, subject, value in created
    ]
    assert [event.id for _, event in bus.events.entries] == [row_id for row_id, *_ in created]
    assert sent(repo) == sorted(row_id for row_id, *_ in created)


@pytest.mark.feature("ledger-event-relay")
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


@pytest.mark.feature("ledger-event-relay")
def test_a_crash_between_publish_and_mark_never_duplicates_the_event(repo, bus):
    first = rows(repo)[0]
    with pytest.raises(Crash) as caught:
        outbox.relay(repo, CrashAfterPublish(bus.events))
    assert str(caught.value) == "crashed before the sent mark"
    assert sent(repo) == []
    assert outbox.relay(repo, bus.events) == 2
    assert [event.id for _, event in bus.events.entries] == [first[0], rows(repo)[1][0]]
    assert sent(repo) == sorted(row_id for row_id, *_ in rows(repo))


def test_a_row_another_relay_marked_while_this_one_published_stays_sent_once(repo, bus):
    class RacingRelay:
        def publish(self, subject, event):
            outbox.relay(repo, bus.events)
            return bus.events.publish(subject, event)

    created = rows(repo)
    assert outbox.relay(repo, RacingRelay(), batch=1) == 1
    assert [event.id for _, event in bus.events.entries] == [row_id for row_id, *_ in created]
    assert sent(repo) == sorted(row_id for row_id, *_ in created)


class RefuseFirst:
    def __init__(self, events):
        self.events = events
        self.refused = []

    def publish(self, subject, event):
        if not self.refused:
            self.refused.append(event.id)
            raise PayloadTooLarge(f"{subject}: payload 1048800 exceeds server limit 1048576")
        self.events.publish(subject, event)


@pytest.mark.feature("ledger-event-relay")
def test_a_row_the_bus_refuses_as_too_large_is_marked_sent_and_never_holds_back_the_rows_after_it(repo, bus, capsys):
    ordered = [row_id for row_id, *_ in rows(repo)]
    refusing = RefuseFirst(bus.events)
    assert outbox.relay(repo, refusing) == 2
    assert refusing.refused == ordered[:1]
    assert [event.id for _, event in bus.events.entries] == ordered[1:]
    assert sent(repo) == sorted(ordered)
    assert f"row {ordered[0]} skipped" in capsys.readouterr().err
    assert outbox.relay(repo, bus.events) == 0


@pytest.mark.feature("ledger-event-relay")
def test_a_row_written_with_fields_outside_the_catalogue_is_published_with_only_the_catalogued_fields(repo, bus):
    fat = create(
        type="phase.changed",
        swarm="ledger",
        entity="phase",
        key="p9",
        version=9,
        author="ledger",
        data={"id": "p9", "title": "Nine", "description": "", "done": False, "comments": ["x" * 2_000_000]},
    )
    with repo.connect() as connection, connection:
        connection.execute(outbox.INSERT, (fat.id, "ledger", 9, "swarm.ledger.phase.p9", fat.encode().decode()))
    outbox.relay(repo, bus.events)
    [published] = [event for _, event in bus.events.entries if event.id == fat.id]
    assert published.data == {"id": "p9", "title": "Nine", "description": "", "done": False}
    assert (published.type, published.key, published.version) == ("phase.changed", "p9", 9)
