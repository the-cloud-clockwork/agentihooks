import os
import uuid

import pytest

from scripts.swarm_ledger.repository import postgres, sqlite
from tests.swarm_ledger.test_hierarchy_reads import PLAN_A, STATE

URL = os.environ.get("LEDGER_TEST_POSTGRES_URL", "")
CONTENT = {
    "title": "Store",
    "overview": "o",
    "sources": [],
    "phases": [{"title": "One", "description": "d"}],
    "tasks": [{"title": "first", "phase": "p1", "lane": "eng"}],
}
CLAIM = {"op": "task_update", "id": "u1", "by": "eng", "item": "tasks/t1", "fields": {"state": "claimed"}}
READS = [
    ("children", None),
    ("children", "plans/a"),
    ("children", "phases/p1"),
    ("subtree", None),
    ("subtree", "plans/a"),
    ("subtree", "slices/s1"),
    ("ancestors", "tasks/t4"),
    ("ancestors", "tasks/t7"),
    ("dependents", "tasks/t1"),
    ("dependents", "tasks/t6"),
]

pytestmark = pytest.mark.skipif(not URL, reason="the Ledger writer lock workflow sets LEDGER_TEST_POSTGRES_URL")


def fresh(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def imported(tmp_path):
    slug = fresh("hierarchy")
    shared = postgres.PostgresLedgerRepository(URL)
    local = sqlite.SQLiteLedgerRepository(tmp_path / sqlite.DATABASE)
    for repository in (shared, local):
        repository.import_document(slug, STATE)
    return shared, local, slug


def test_every_hierarchy_read_on_postgres_matches_the_sqlite_repository(imported):
    shared, local, slug = imported
    assert [(row["node"], row["depth"]) for row in shared.nodes(slug, "subtree", "plans/a")] == PLAN_A
    for read, node in READS:
        assert shared.nodes(slug, read, node) == local.nodes(slug, read, node), (read, node)
    assert shared.rebuild(slug) == local.rebuild(slug)


def test_an_imported_postgres_ledger_exports_reads_and_snapshots_as_stored(imported):
    shared, local, slug = imported
    assert shared.exists(slug)
    assert shared.export_document(slug) == local.export_document(slug)
    assert shared.read(slug, "tasks/t2") == local.read(slug, "tasks/t2")
    assert shared.token(slug)
    assert slug in [summary["slug"] for summary in shared.list_summaries()]
    with shared.snapshot() as snapshot, local.snapshot() as stored:
        assert slug in snapshot.slugs()
        assert snapshot.document(slug) == shared.get_document(slug)
        assert snapshot.hierarchy(slug) == stored.hierarchy(slug)


def test_a_postgres_ledger_write_advances_its_revision_and_records_its_events():
    slug = fresh("writes")
    shared = postgres.PostgresLedgerRepository(URL)
    assert shared.create(slug, CONTENT)
    created = shared.get_document(slug)["_meta"]["rev"]
    claimed, rejected = shared.apply_ops(slug, ops=[CLAIM])
    assert rejected == []
    assert claimed["_meta"]["rev"] > created
    assert shared.get_document(slug)["tasks"][0]["state"] == "claimed"
    assert shared.events_since(slug, created)
    with pytest.raises(sqlite.Missing):
        shared.events_since(fresh("missing"), 0)


def test_the_postgres_relay_journal_holds_each_revision_once_and_drops_it():
    slug = fresh("journal")
    shared = postgres.PostgresLedgerRepository(URL)
    for _ in range(2):
        shared.hold_revision("inbox", slug, 3)
    for revision in (5, 7):
        shared.hold_revision("inbox", slug, revision)
    assert shared.held_revisions()["inbox"][slug] == [3, 5, 7]
    shared.release_revisions("inbox", slug, 4, 5)
    assert shared.held_revisions()["inbox"][slug] == [3, 7]
    shared.drop_revisions("inbox", slug)
    assert slug not in shared.held_revisions().get("inbox", {})
