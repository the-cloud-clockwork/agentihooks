import os
import uuid
from contextlib import closing

import psycopg2
import pytest

from scripts.events import memory
from scripts.events.config import BusConfig
from scripts.swarm_ledger.repository import outbox, postgres, sqlite

URL = os.environ.get("LEDGER_TEST_POSTGRES_URL", "")
CONTENT = {
    "title": "Store",
    "overview": "o",
    "sources": [],
    "phases": [{"title": "One", "description": "d"}],
    "tasks": [{"title": "first", "phase": "p1", "lane": "eng"}],
}
CLAIM = {"op": "task_update", "id": "u1", "by": "eng", "item": "tasks/t1", "fields": {"state": "claimed"}}

pytestmark = pytest.mark.skipif(not URL, reason="the Ledger writer lock workflow sets LEDGER_TEST_POSTGRES_URL")


def subjects(slug):
    with closing(psycopg2.connect(URL)) as reader, reader.cursor() as cursor:
        cursor.execute("SELECT subject, revision FROM outbox WHERE slug = %s ORDER BY revision, subject", (slug,))
        return cursor.fetchall()


@pytest.fixture
def ledger():
    slug = f"outbox-{uuid.uuid4().hex[:12]}"
    repository = postgres.PostgresLedgerRepository(URL)
    repository.create(slug, CONTENT)
    return repository, slug


def test_a_postgres_task_and_phase_change_writes_outbox_rows_in_the_write_transaction(ledger):
    repository, slug = ledger
    created = repository.get_document(slug)["_meta"]["rev"]
    assert subjects(slug) == [(f"swarm.{slug}.phase.p1", created), (f"swarm.{slug}.task.t1", created)]
    claimed, _ = repository.apply_ops(slug, ops=[CLAIM])
    done, _ = repository.apply_ops(slug, changes=[{"path": "phases/p1/done", "value": True, "base": False}])
    assert subjects(slug)[2:] == [
        (f"swarm.{slug}.task.t1", claimed["_meta"]["rev"]),
        (f"swarm.{slug}.phase.p1", done["_meta"]["rev"]),
    ]


def test_a_failed_postgres_ledger_write_leaves_neither_the_change_nor_its_outbox_row(ledger, monkeypatch):
    repository, slug = ledger
    before = repository.get_document(slug)
    created = subjects(slug)

    def broken(slug, state):
        raise RuntimeError("summary failed")

    monkeypatch.setattr(sqlite, "summarize", broken)
    with pytest.raises(RuntimeError) as caught:
        repository.apply_ops(slug, ops=[CLAIM])
    assert str(caught.value) == "summary failed"
    monkeypatch.undo()
    assert subjects(slug) == created
    assert postgres.PostgresLedgerRepository(URL).get_document(slug) == before


def test_the_relay_publishes_postgres_outbox_rows_once_and_marks_them_sent(ledger):
    repository, slug = ledger
    bus = memory.open_bus(BusConfig(), clock=lambda: 1_000)
    pending = [subject for subject, _ in subjects(slug)]
    drained = [outbox.relay(repository, bus.events, batch=1000) for _ in range(20)]
    assert drained[-1] == 0
    assert [subject for subject, _ in bus.events.entries if subject.startswith(f"swarm.{slug}.")] == pending
    with closing(psycopg2.connect(URL)) as reader, reader.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM outbox WHERE slug = %s AND id NOT IN (SELECT id FROM outbox_sent)", (slug,)
        )
        assert cursor.fetchone() == (0,)


def test_postgres_prune_deletes_sent_rows_and_marks_older_than_the_window_and_keeps_the_rest(ledger):
    repository, slug = ledger
    with closing(psycopg2.connect(URL)) as writer, writer, writer.cursor() as cursor:
        cursor.execute("INSERT INTO outbox_sent SELECT id, 1000000 FROM outbox WHERE slug = %s", (slug,))
    repository.apply_ops(slug, ops=[CLAIM])
    claimed = subjects(slug)[-1]
    outbox.prune(repository, 120_000, clock=lambda: 1_120_001)
    assert subjects(slug) == [claimed]
    with closing(psycopg2.connect(URL)) as reader, reader.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM outbox_sent WHERE sent_at < 1120001")
        assert cursor.fetchone() == (0,)


def test_a_postgres_mark_table_without_the_sent_time_gains_it_with_old_marks_at_zero():
    name = f"legacy_{uuid.uuid4().hex[:12]}"
    admin = psycopg2.connect(URL)
    admin.autocommit = True
    with closing(admin), admin.cursor() as cursor:
        cursor.execute(f"CREATE DATABASE {name}")
    legacy = f"{URL.rsplit('/', 1)[0]}/{name}"
    try:
        with closing(psycopg2.connect(legacy)) as writer, writer, writer.cursor() as cursor:
            cursor.execute("CREATE TABLE outbox_sent (id TEXT PRIMARY KEY)")
            cursor.execute("INSERT INTO outbox_sent VALUES ('old')")
        postgres.PostgresLedgerRepository(legacy).create("legacy", CONTENT)
        with closing(psycopg2.connect(legacy)) as reader, reader.cursor() as cursor:
            cursor.execute("SELECT id, sent_at FROM outbox_sent WHERE id = 'old'")
            assert cursor.fetchall() == [("old", 0)]
    finally:
        admin = psycopg2.connect(URL)
        admin.autocommit = True
        with closing(admin), admin.cursor() as cursor:
            cursor.execute(f"DROP DATABASE {name} WITH (FORCE)")
