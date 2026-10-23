import os
import uuid
from contextlib import closing

import psycopg2
import pytest

from scripts.swarm_ledger.repository import postgres, sqlite

URL = os.environ.get("LEDGER_TEST_POSTGRES_URL", "")
CONTENT = {
    "title": "Floor",
    "overview": "o",
    "sources": [],
    "phases": [{"title": "One", "description": "d"}],
    "tasks": [{"title": "first", "phase": "p1", "lane": "eng"}],
}
CLAIM = {"op": "task_update", "id": "u1", "by": "eng", "item": "tasks/t1", "fields": {"state": "claimed"}}
DONE = {"path": "phases/p1/done", "value": True, "base": False}

pytestmark = pytest.mark.skipif(not URL, reason="the Ledger writer lock workflow sets LEDGER_TEST_POSTGRES_URL")


def run(sql, parameters=()):
    with closing(psycopg2.connect(URL)) as raw, raw, raw.cursor() as cursor:
        cursor.execute(sql, parameters)
        return cursor.fetchall() if cursor.description else None


def generation(slug):
    return run("SELECT generation FROM ledgers WHERE slug = %s", (slug,))[0][0]


def refusal(version, required):
    return (
        f"ledger writer version {version} is below version {required}, the oldest the shared database allows;"
        " update its code and restart it"
    )


@pytest.fixture
def ledger():
    slug = f"floor-{uuid.uuid4().hex[:12]}"
    repository = postgres.PostgresLedgerRepository(URL)
    repository.create(slug, CONTENT)
    return repository, slug


@pytest.fixture
def raised_floor():
    run("UPDATE writer_floor SET version = %s WHERE name = 'ledger'", (postgres.VERSION + 1,))
    yield postgres.VERSION + 1
    run("UPDATE writer_floor SET version = %s WHERE name = 'ledger'", (postgres.REQUIRED,))


def test_a_current_writer_stores_the_required_version_and_writes(ledger):
    repository, slug = ledger
    before = generation(slug)
    repository.apply_ops(slug, ops=[CLAIM])
    assert run("SELECT version FROM writer_floor WHERE name = 'ledger'") == [(postgres.REQUIRED,)]
    assert postgres.VERSION >= postgres.REQUIRED
    assert generation(slug) == before + 1


def test_a_writer_below_the_stored_version_is_refused_and_the_refusal_becomes_a_ledger_alert(ledger, raised_floor):
    repository, slug = ledger
    before = generation(slug)
    with pytest.raises(postgres.Refused) as refused:
        repository.apply_ops(slug, ops=[CLAIM])
    assert str(refused.value) == refusal(postgres.VERSION, raised_floor)
    assert generation(slug) == before
    run("UPDATE writer_floor SET version = %s WHERE name = 'ledger'", (postgres.REQUIRED,))
    repository.apply_ops(slug, changes=[DONE])
    alerts = [a for a in repository.get_document(slug)["alerts"] if a["source"] == "writer"]
    assert [(a["state"], a["target"]) for a in alerts] == [("open", "master")]
    assert refusal(postgres.VERSION, raised_floor) in alerts[0]["text"]


def test_a_writer_on_code_without_the_check_is_refused_and_no_replica_rebuilds_in_full(ledger, monkeypatch):
    repository, slug = ledger
    replica = postgres.PostgresLedgerRepository(URL)
    cached = replica.get_document(slug)
    before = generation(slug)
    with closing(psycopg2.connect(URL)) as old:
        with pytest.raises(psycopg2.Error) as refused, old, old.cursor() as cursor:
            cursor.execute("UPDATE ledgers SET generation = generation + 1 WHERE slug = %s", (slug,))
        assert refused.value.diag.message_primary == refusal(0, postgres.REQUIRED)
        assert generation(slug) == before

        def whole(*args):
            raise AssertionError("the replica rebuilt the whole ledger")

        monkeypatch.setattr(sqlite, "read_rows", whole)
        assert replica.get_document(slug) == cached
        monkeypatch.undo()
        repository.apply_ops(slug, changes=[DONE])
    texts = [a["text"] for a in repository.get_document(slug)["alerts"] if a["source"] == "writer"]
    assert f"The ledger writer at 127.0.0.1 was refused: {refusal(0, postgres.REQUIRED)}" in texts


def test_a_writer_on_code_without_the_check_passes_when_it_records_its_changed_rows(ledger):
    repository, slug = ledger
    after = generation(slug) + 1
    with closing(psycopg2.connect(URL)) as raw, raw, raw.cursor() as cursor:
        cursor.execute("INSERT INTO row_changes VALUES (%s, %s, '[]')", (slug, after))
        cursor.execute("UPDATE ledgers SET generation = %s WHERE slug = %s", (after, slug))
    assert generation(slug) == after
    assert postgres.PostgresLedgerRepository(URL).get_document(slug)["title"] == "Floor"
