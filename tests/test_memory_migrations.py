"""Tests for SV2-RET-03 staged session catalog migrations."""

import hashlib
import json
from pathlib import Path

import pytest

from hooks.memory import migrations
from hooks.memory.catalog import Conflict
from tests import sv2_ret03_cases as cases

pytestmark = pytest.mark.unit
EVIDENCE = Path(__file__).parents[1] / "evidence" / "SV2-RET-03"
ATTRIBUTED = "event_id = 's-attributed-e1'"
UNATTRIBUTED = "event_id = 's-unattributed-e1'"


@pytest.fixture
def world(tmp_path):
    found = cases.World(tmp_path)
    yield found
    found.catalog.close()


def provenance_rows(world) -> int:
    return world.catalog.db.execute("SELECT COUNT(*) FROM session_event_provenance").fetchone()[0]


def test_previous_schema_opens_with_every_event_remaining_and_no_ledger(world):
    assert world.migrator.status() == {
        "state": migrations.PENDING,
        "migration_id": migrations.PROVENANCE,
        "report": None,
        "migration_rows_remaining": 9,
    }
    assert world.previous_reader() == {"columns": 10, "rows": 9}


def test_add_records_the_ledger_once_with_the_event_count(world):
    assert world.migrator.add() is True
    assert world.migrator.add() is False
    status = world.migrator.status()
    assert (status["state"], status["revision"], status["rows_before"], status["after_rowid"]) == ("added", 0, 9, 0)


def test_new_writer_records_provenance_in_the_event_transaction(world):
    world.window()
    rows = world.catalog.db.execute(
        "SELECT event_id, trust_class, project_state FROM session_event_provenance ORDER BY event_id"
    ).fetchall()
    assert rows == [("s-attributed-e6", "original", "attributed"), ("s-attributed-e7", "original", "attributed")]
    assert world.migrator.status()["migration_rows_remaining"] == 10


def test_backfill_writes_unknown_for_unattributed_events_and_keeps_project_ids(world):
    world.migrator.add()
    status = world.backfill()
    assert status["state"] == migrations.BACKFILLED
    assert status["rows_done"] == 9
    assert status["migration_rows_remaining"] == 0
    states = dict(world.catalog.db.execute("SELECT event_id, project_state FROM session_event_provenance").fetchall())
    assert states["s-unattributed-e1"] == "unknown"
    assert states["s-attributed-e1"] == "attributed"
    project_ids = world.catalog.db.execute("SELECT DISTINCT project_id FROM session_event ORDER BY 1").fetchall()
    assert project_ids == [(None,), ("proj-alpha",)]


def test_backfill_advances_one_batch_per_step_with_a_durable_cursor(world):
    world.migrator.add()
    status = world.migrator.backfill(0, 2, 1)
    assert (status["after_rowid"], status["rows_done"], status["revision"], status["state"]) == (2, 2, 1, "added")
    assert status["migration_rows_remaining"] == 7
    status = world.migrator.backfill(1, 4, 1)
    assert (status["after_rowid"], status["rows_done"], status["revision"]) == (6, 6, 2)


def test_backfill_rewinds_to_fill_rows_below_its_cursor(world):
    world.migrator.add()
    world.backfill()
    world.catalog.db.execute("DELETE FROM session_event_provenance WHERE event_id = 's-attributed-e1'")
    status = world.backfill()
    assert status["state"] == migrations.BACKFILLED
    assert status["migration_rows_remaining"] == 0
    assert status["rows_done"] == 10
    assert provenance_rows(world) == 9


def test_backfill_with_a_stale_revision_changes_nothing(world):
    world.migrator.add()
    world.migrator.backfill(0, 2, 1)
    with pytest.raises(Conflict, match="stale"):
        world.migrator.backfill(0, 2, 1)
    assert world.migrator.status()["rows_done"] == 2
    assert provenance_rows(world) == 2


def test_backfill_before_add_and_while_active_is_refused(world):
    with pytest.raises(Conflict, match="has not been added"):
        world.migrator.backfill(0, 2, 1)
    world.migrator.add()
    world.backfill()
    world.staged("verify")
    world.staged("activate")
    with pytest.raises(Conflict, match="deactivate"):
        world.backfill()


@pytest.mark.parametrize(("size", "batches"), [(0, 1), (1, 0)])
def test_backfill_needs_positive_limits(world, size, batches):
    world.migrator.add()
    with pytest.raises(ValueError):
        world.migrator.backfill(0, size, batches)
    assert world.migrator.status()["revision"] == 0


def test_verify_passes_a_complete_backfill_and_records_its_report(world):
    world.migrator.add()
    world.window()
    world.backfill()
    status = world.staged("verify")
    assert status["state"] == migrations.VERIFIED
    assert status["report"] == {
        "duplicate_event_sequences": 0,
        "inconsistent_provenance": 0,
        "migration_rows_remaining": 0,
        "orphan_provenance": 0,
        "passed": True,
        "rows_before": 9,
        "rows_lost": 0,
        "rows_now": 12,
        "unresolved_sources": 0,
    }


@pytest.mark.parametrize(
    ("damage", "failed", "count"),
    [
        (
            f"UPDATE session_event_provenance SET project_state = 'attributed' WHERE {UNATTRIBUTED}",
            "inconsistent_provenance",
            1,
        ),
        (
            f"UPDATE session_event_provenance SET trust_class = 'synthesized' WHERE {ATTRIBUTED}",
            "inconsistent_provenance",
            1,
        ),
        ("DELETE FROM session_source WHERE session_uid = 's-unattributed'", "unresolved_sources", 4),
        (f"DELETE FROM session_event WHERE {ATTRIBUTED}", "rows_lost", 1),
    ],
)
def test_verify_refuses_activation_when_a_check_fails(world, damage, failed, count):
    world.migrator.add()
    world.backfill()
    world.catalog.db.execute(damage)
    status = world.staged("verify")
    assert status["state"] == migrations.BACKFILLED
    assert status["report"]["passed"] is False
    assert status["report"][failed] == count
    with pytest.raises(Conflict, match="activated"):
        world.staged("activate")


def test_verify_counts_orphan_provenance_and_remaining_rows(world):
    world.migrator.add()
    world.backfill()
    world.catalog.db.execute("DELETE FROM session_event WHERE event_id = 's-attributed-e1'")
    world.catalog.db.execute("DELETE FROM session_event_provenance WHERE event_id = 's-attributed-e2'")
    report = world.staged("verify")["report"]
    assert (report["orphan_provenance"], report["migration_rows_remaining"], report["rows_lost"]) == (1, 1, 1)


def test_verify_before_backfill_is_refused(world):
    world.migrator.add()
    with pytest.raises(Conflict, match="backfilled"):
        world.staged("verify")


def test_activate_refuses_rows_written_after_verification(world):
    world.migrator.add()
    world.backfill()
    world.staged("verify")
    world.previous_write("s-unattributed", "g1", range(9, 10))
    with pytest.raises(Conflict, match="activated"):
        world.staged("activate")
    status = world.backfill()
    assert status["state"] == migrations.BACKFILLED
    assert world.staged("verify")["state"] == migrations.VERIFIED
    assert world.staged("activate")["state"] == migrations.ACTIVE


def test_consumers_read_provenance_only_while_active(world):
    world.migrator.add()
    world.backfill()
    world.staged("verify")
    with pytest.raises(Conflict, match="not active"):
        world.migrator.provenance(world.domain, "s-attributed-e1")
    world.staged("activate")
    assert world.migrator.provenance(world.domain, "s-attributed-e1") == {
        "trust_class": "original",
        "project_state": "attributed",
        "recorded": True,
    }
    assert world.migrator.provenance(world.domain, "missing") is None
    assert world.migrator.provenance("other-domain", "s-attributed-e1") is None
    world.staged("deactivate")
    with pytest.raises(Conflict, match="not active"):
        world.migrator.provenance(world.domain, "s-attributed-e1")
    assert provenance_rows(world) == 9


@pytest.mark.parametrize(("uid", "project_state"), [("s-unattributed", "unknown"), ("s-attributed", "attributed")])
def test_a_previous_writer_after_activation_reads_as_unrecorded(world, uid, project_state):
    world.migrator.add()
    world.backfill()
    world.staged("verify")
    world.staged("activate")
    world.previous_write(uid, "g1", range(20, 21))
    assert world.migrator.provenance(world.domain, f"{uid}-e20") == {
        "trust_class": "original",
        "project_state": project_state,
        "recorded": False,
    }
    assert world.migrator.status()["migration_rows_remaining"] == 1


def test_deactivate_needs_an_active_migration(world):
    world.migrator.add()
    with pytest.raises(Conflict, match="deactivated"):
        world.staged("deactivate")


def test_activate_with_a_stale_revision_is_refused(world):
    world.migrator.add()
    world.backfill()
    world.staged("verify")
    revision = world.migrator.status()["revision"]
    with pytest.raises(Conflict, match="stale"):
        world.migrator.activate(revision - 1)
    assert world.migrator.status()["state"] == migrations.VERIFIED


def test_startup_reports_the_dimension_mismatch_without_touching_vectors(world):
    vectors = world.vectors()
    started = world.migrator.startup()
    assert started["added"] == [migrations.PROVENANCE]
    assert started["blocked"] == [
        {
            "migration_id": migrations.DIMENSION,
            "access_domain": world.domain,
            "index_generation": "idx-v1",
            "dimension": 3,
            "mismatched_vectors": 2,
            "requires": "operator",
        }
    ]
    assert world.migrator.startup()["added"] == []
    assert world.vectors() == vectors
    assert world.work() == []


def test_startup_without_index_tables_blocks_nothing(tmp_path):
    from hooks.memory.catalog import Catalog

    catalog = Catalog(tmp_path / "plain.sqlite3")
    migrator = migrations.Migrator(catalog)
    assert migrator.startup()["blocked"] == []
    approval = migrations.Approval("op", "operator", migrations.DIMENSION, "d", "g")
    assert migrator.dimensions.drop(approval, 1.0) == {"dropped": [], "requeued": []}
    catalog.close()


@pytest.mark.parametrize(
    "fields",
    [
        {"role": "engineer"},
        {"actor": ""},
        {"migration_id": migrations.PROVENANCE},
        {"access_domain": ""},
        {"index_generation": ""},
    ],
)
def test_destructive_vector_change_needs_an_operator_approval(world, fields):
    protected = world.protected()
    with pytest.raises(Conflict, match="operator approval"):
        world.migrator.dimensions.drop(world.approval(**fields), 1.0)
    assert world.protected() == protected


def test_operator_approval_drops_only_scoped_mismatches_and_requeues_them(world):
    world.index.register(world.domain, "idx-v2", cases.EmbeddingModel("concept-4", 4))
    world.catalog.db.execute(
        "INSERT INTO chunk_vector SELECT access_domain, 'idx-v2', chunk_id, session_uid, chunker_version, "
        "content_hash, model, dimension, vector FROM chunk_vector WHERE chunk_id = 'c-current'"
    )
    answer = world.migrator.dimensions.drop(world.approval(), 42.0)
    assert answer == {"dropped": ["c-legacy-1", "c-legacy-2"], "requeued": ["c-legacy-1", "c-legacy-2"]}
    assert world.vectors() == [["c-current", 3], ["c-current", 3]]
    assert world.work() == [["c-legacy-1", 42.0], ["c-legacy-2", 42.0]]
    queued = world.catalog.db.execute(
        "SELECT index_generation, session_uid, chunker_version, content_hash, attempts FROM embedding_work "
        "ORDER BY chunk_id"
    ).fetchall()
    assert queued == [
        ("idx-v1", "s-attributed", "chunker-v1", "hash-c-legacy-1", 0),
        ("idx-v1", "s-unattributed", "chunker-v1", "hash-c-legacy-2", 0),
    ]
    blocked = world.migrator.dimensions.blocked()
    assert [(item["index_generation"], item["mismatched_vectors"]) for item in blocked] == [("idx-v2", 1)]


@pytest.mark.parametrize("state", ["restricted", "deleted"])
def test_operator_drop_never_requeues_a_session_that_is_not_retained(world, state):
    world.catalog.set_state(world.domain, "s-unattributed", state, 1, 10.0)
    answer = world.migrator.dimensions.drop(world.approval(), 42.0)
    assert answer == {"dropped": ["c-legacy-1", "c-legacy-2"], "requeued": ["c-legacy-1"]}
    assert world.work() == [["c-legacy-1", 42.0]]


def test_interrupted_backfill_rolls_back_its_open_batch(world):
    world.migrator.add()
    with pytest.raises(cases.Interrupted):
        world.backfill(cases.Interrupting(world.catalog, 1))
    status = world.migrator.status()
    assert (status["after_rowid"], status["rows_done"], status["revision"]) == (2, 2, 1)
    assert provenance_rows(world) == 2


@pytest.mark.parametrize("case", ["A", "B", "C"])
def test_package_cases_match_committed_evidence(tmp_path, case):
    observed = json.loads(json.dumps(cases.run_case(case, tmp_path)))
    path = EVIDENCE / f"{case.lower()}-result.json"
    expected = json.loads(path.read_text()) if path.exists() else None
    assert observed == expected, json.dumps(observed, sort_keys=True)


def test_package_record_and_manifest_match_the_fixture_inputs():
    record = json.loads((EVIDENCE / "result.json").read_text())
    manifest = json.loads((EVIDENCE / "manifest.json").read_text())
    assert manifest["input_sha256"] == {cases.FIXTURE.name: hashlib.sha256(cases.FIXTURE.read_bytes()).hexdigest()}
    assert manifest["fixture_inputs"] == record["fixture_inputs"] == cases.FIXTURE_INPUTS
    assert manifest["tested_commit"] == record["tested_commit"]
