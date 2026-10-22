import os
import uuid
from contextlib import closing

import psycopg2
import pytest

from scripts.swarm_ledger.repository import postgres, sqlite

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
