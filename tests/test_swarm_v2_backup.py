"""Tests for SV2-RET-04 backup manifests and restore drills."""

import dataclasses
import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from hooks.memory import archive, index_worker
from scripts.swarm_v2 import backup
from tests import sv2_ret04_cases as cases

pytestmark = pytest.mark.unit
EVIDENCE = Path(__file__).parents[1] / "evidence" / "SV2-RET-04"
READS_BEFORE_OBJECTS = 1 + len(backup.FILES)


@pytest.fixture
def world(tmp_path):
    return cases.World(tmp_path)


def manifest(world, set_id):
    return json.loads(world.backup.get(set_id))


def test_capture_records_every_component_with_its_version_and_key_id(world):
    found = manifest(world, world.capture())
    assert (found["format"], found["corpus"]) == (backup.FORMAT, world.domain)
    assert found["captured_at"] == world.doc["clock"]["captured"]
    assert sorted(found["components"]) == sorted((backup.CATALOG, *backup.FILES))
    for name, component in found["components"].items():
        assert component["version"] == world.doc["versions"][name]
        assert component["key_id"] == world.doc["key_ids"][name]
        assert component["bytes"] == len(world.backup.get(component["digest"]))
    for name in backup.FILES:
        assert world.backup.get(found["components"][name]["digest"]).decode() == world.doc["files"][name]


def test_capture_reports_a_missing_source_object_and_never_fabricates_it(world):
    found = manifest(world, world.capture())
    (lost,) = world.lost
    assert found["objects"][lost] == archive.MISSING
    with pytest.raises(archive.EvidenceIntegrityError):
        world.backup.get(lost)
    present = [digest for digest, state in found["objects"].items() if state == backup.PRESENT]
    assert all(world.backup.get(digest) == world.objects.get(digest) for digest in present)
    assert set(world.checkpoint) <= set(present)


def test_capture_omits_projection_tables_from_the_catalog_copy(world, tmp_path):
    found = manifest(world, world.capture())
    copy = tmp_path / "copy.sqlite3"
    copy.write_bytes(world.backup.get(found["components"][backup.CATALOG]["digest"]))
    tables = {row[0] for row in sqlite3.connect(copy).execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not tables & set(backup.PROJECTIONS)
    assert {"session_event", "archive_manifest"} <= tables
    assert found["omitted_projections"] == list(backup.PROJECTIONS)


def test_the_set_id_is_the_digest_of_its_manifest(world):
    set_id = world.capture()
    assert hashlib.sha256(world.backup.get(set_id)).hexdigest() == set_id


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"withhold": "fixture-key-brain"}, backup.NO_KEY),
        ({"corpus": "personal-corpus"}, backup.FORBIDDEN),
        ({"versions": {"ledger": frozenset({"ledger-2"})}}, backup.OLD_VERSION),
        ({"versions": {"catalog": frozenset()}}, backup.OLD_VERSION),
    ],
)
def test_restore_refuses_access_it_cannot_honour_before_writing(world, changes, message):
    set_id, target = world.capture(), world.root / "restore"
    with pytest.raises(backup.Refused) as refused:
        backup.restore(world.backup, set_id, target, world.access(**changes))
    assert str(refused.value) == message
    assert not target.exists()


def test_restore_refuses_another_major_format_before_writing(world):
    found = manifest(world, world.capture())
    future = world.backup.put(json.dumps({**found, "format": "2.0"}).encode())
    with pytest.raises(backup.Refused, match="major version"):
        backup.restore(world.backup, future, world.root / "restore", world.access())
    assert not (world.root / "restore").exists()


def test_restore_accepts_a_newer_minor_format(world):
    found = manifest(world, world.capture())
    newer = world.backup.put(json.dumps({**found, "format": "1.7"}).encode())
    restored = backup.restore(world.backup, newer, world.root / "restore", world.access())
    restored.catalog.close()
    assert restored.manifest["format"] == "1.7"


def test_restore_refuses_a_folder_holding_other_data(world):
    set_id, target = world.capture(), world.root / "occupied"
    target.mkdir()
    (target / "other").write_text("kept")
    with pytest.raises(backup.Refused, match="empty folder"):
        backup.restore(world.backup, set_id, target, world.access())
    assert sorted(path.name for path in target.iterdir()) == ["other"]


def test_restore_ignores_a_partial_write_left_by_a_killed_attempt(world):
    set_id, target = world.capture(), world.root / "restore"
    target.mkdir()
    (target / f"{backup.PARTIAL_WRITE}abc").write_bytes(b"half")
    backup.restore(world.backup, set_id, target, world.access()).catalog.close()
    assert json.loads((target / backup.MARKER).read_text()) == {"set_id": set_id}


def test_restore_refuses_a_folder_claimed_by_another_set(world):
    first, second = world.capture(), world.capture("later_capture")
    target = world.root / "restore"
    backup.restore(world.backup, first, target, world.access()).catalog.close()
    with pytest.raises(backup.Refused, match="empty folder"):
        backup.restore(world.backup, second, target, world.access())


def test_restore_resumes_into_its_own_folder_without_duplicates(world):
    set_id, target = world.capture(), world.root / "restore"
    with pytest.raises(cases.Interrupted):
        backup.restore(cases.Interrupting(world.backup.root, READS_BEFORE_OBJECTS + 1), set_id, target, world.access())
    assert cases.object_files(target) == 1
    assert not (target / backup.CATALOG_FILE).exists()
    restored = backup.restore(world.backup, set_id, target, world.access())
    restored.catalog.close()
    assert cases.object_files(target) == cases.present(world, set_id)
    assert restored.irrecoverable == {world.lost[0]: archive.MISSING}


def test_drill_rebuilds_projections_answers_the_query_and_recovers_the_checkpoint(world):
    set_id = world.capture()
    report = backup.drill(world.backup, set_id, world.plan())
    assert report["set_id"] == set_id
    assert report[backup.METRIC] == backup.PARTIAL
    assert report["query_answered"] is True
    assert report["checkpoint"] == {"task_id": "fixture-task", "objects": 2, "recovered": True}
    assert report["rebuilt_projections"] == {"sessions": 2, "state": "drained", "pending": 0}
    assert report["recovery_point"] == world.doc["clock"]["captured"]
    assert report["recovery_time_seconds"] == 2 * world.doc["clock"]["step"]
    assert world.restored_rows()[0] == world.source_rows()


def test_drill_is_verified_when_no_source_object_is_missing(world):
    world.heal()
    report = backup.drill(world.backup, world.capture(), world.plan())
    assert report[backup.METRIC] == backup.VERIFIED
    assert report["irrecoverable"] == {}


def test_drill_fails_when_the_query_cannot_be_answered(world):
    set_id = world.capture()
    world.doc["query_session"] = cases.lost_session(world.doc)["session_uid"]
    report = backup.drill(world.backup, set_id, world.plan())
    assert report[backup.METRIC] == backup.FAILED
    assert report["query_answered"] is False


def test_capture_binds_the_source_answer_to_the_manifest(world):
    found = manifest(world, world.capture())
    catalog = cases.Catalog(world.catalog_path)
    try:
        answer = world.query(catalog, world.objects)
    finally:
        catalog.close()
    assert found["query_sha256"] == hashlib.sha256(backup._encoded(answer)).hexdigest()
    assert answer["complete"] is True and answer["events"] == 4


@pytest.mark.parametrize("stub", [{}, {"complete": True, "events": 4, "first_record": "invented"}])
def test_a_query_whose_answer_differs_from_capture_fails_the_drill(world, stub):
    set_id = world.capture()
    entry = world.registry.drill(world.backup, set_id, 1, world.plan(query=lambda catalog, store: stub))
    assert entry["status"] == backup.FAILED
    assert world.registry.accepted(set_id) is False


def test_restore_refuses_a_target_that_is_a_file(world):
    set_id, target = world.capture(), world.root / "file-target"
    target.write_text("kept")
    with pytest.raises(backup.Refused, match="empty folder"):
        backup.restore(world.backup, set_id, target, world.access())
    assert target.read_text() == "kept"


def test_drill_fails_when_the_designated_checkpoint_is_unknown(world):
    report = backup.drill(world.backup, world.capture(), world.plan(task_id="other-task"))
    assert report[backup.METRIC] == backup.FAILED
    assert report["checkpoint"] == {"task_id": "other-task", "objects": 0, "recovered": False}


def test_drill_fails_when_a_designated_checkpoint_object_is_missing(world):
    world.objects._path(world.checkpoint[1]).unlink()
    report = backup.drill(world.backup, world.capture(), world.plan())
    assert report[backup.METRIC] == backup.FAILED
    assert report["checkpoint"]["recovered"] is False
    assert report["irrecoverable"][world.checkpoint[1]] == archive.MISSING


def test_a_checkpoint_object_altered_after_restore_does_not_recover(world):
    restored = backup.restore(world.backup, world.capture(), world.root / "restore", world.access())
    restored.store._path(world.checkpoint[0]).write_bytes(b"altered")
    try:
        assert backup._checkpoint(restored, "fixture-task")["recovered"] is False
    finally:
        restored.catalog.close()


def test_drill_fails_on_an_altered_backup_component(world):
    set_id = world.capture()
    digest = manifest(world, set_id)["components"]["brain"]["digest"]
    world.backup._path(digest).write_bytes(b"altered")
    report = backup.drill(world.backup, set_id, world.plan())
    expected = {"set_id": set_id, backup.METRIC: backup.FAILED, "reason": archive.ALTERED}
    assert report == {**expected, "recovery_time_seconds": world.doc["clock"]["step"]}


def test_a_refused_drill_raises_and_records_nothing(world):
    set_id = world.capture()
    before = world.registry.path.read_bytes()
    with pytest.raises(backup.Refused, match="encryption key"):
        world.registry.drill(world.backup, set_id, 1, world.plan(access=world.access(withhold="fixture-key-brain")))
    assert world.registry.path.read_bytes() == before


def test_an_upload_without_a_drill_is_not_accepted(world):
    set_id = world.capture()
    assert world.registry.accepted(set_id) is False
    assert world.registry.preserved() is None
    assert world.registry.retention_allowed() is False
    assert world.registry.status()[backup.METRIC] == {"uploaded": 1, "verified": 0, "partial": 0, "failed": 0}


def test_record_replays_without_resetting_a_drilled_set(world):
    set_id = world.capture()
    world.registry.drill(world.backup, set_id, 1, world.plan())
    assert world.registry.record(set_id, 0.0)["status"] == backup.PARTIAL


def test_a_registry_drill_records_the_report_it_ran(world):
    set_id = world.capture()
    entry = world.registry.drill(world.backup, set_id, 1, world.plan())
    report = backup.drill(world.backup, set_id, world.plan("again"))
    assert entry["report"] == backup.recorded(report)
    assert (entry["status"], entry["attempt"], entry["accepted_attempt"]) == (backup.PARTIAL, 1, 1)


@pytest.mark.parametrize(
    ("prepare", "status", "accepted", "retention"),
    [("heal", backup.VERIFIED, True, True), (None, backup.PARTIAL, True, False), ("lose", backup.FAILED, False, False)],
)
def test_acceptance_and_retention_follow_the_drill_status(world, prepare, status, accepted, retention):
    if prepare == "heal":
        world.heal()
    set_id = world.capture()
    if prepare == "lose":
        world.doc["query_session"] = cases.lost_session(world.doc)["session_uid"]
    assert world.registry.drill(world.backup, set_id, 1, world.plan())["status"] == status
    assert world.registry.accepted(set_id) is accepted
    assert world.registry.retention_allowed() is retention
    assert (world.registry.preserved() == set_id) is accepted


def test_a_newer_unverified_set_stops_retention_and_keeps_the_verified_set(world):
    world.heal()
    verified = world.capture()
    world.registry.drill(world.backup, verified, 1, world.plan())
    newer = world.capture("later_capture")
    assert (world.registry.retention_allowed(), world.registry.preserved()) == (False, verified)
    world.registry.drill(world.backup, newer, 1, world.plan("newer"))
    assert (world.registry.retention_allowed(), world.registry.preserved()) == (True, newer)


def test_a_failed_redrill_keeps_the_set_preserved_but_not_accepted(world):
    set_id = world.capture()
    world.registry.drill(world.backup, set_id, 1, world.plan())
    world.backup._path(manifest(world, set_id)["components"]["brain"]["digest"]).write_bytes(b"damaged")
    assert world.registry.drill(world.backup, set_id, 2, world.plan("again"))["status"] == backup.FAILED
    assert world.registry.accepted(set_id) is False
    assert world.registry.preserved() == set_id


def test_a_stale_or_unknown_drill_changes_nothing(world):
    set_id = world.capture()
    world.registry.drill(world.backup, set_id, 2, world.plan())
    before = world.registry.path.read_bytes()
    with pytest.raises(backup.Refused, match="newer restore drill"):
        world.registry.drill(world.backup, set_id, 1, world.plan("older"))
    assert not (world.root / "older").exists()
    with pytest.raises(backup.Refused, match="different result"):
        world.registry.drill(world.backup, set_id, 2, world.plan("conflict", task_id="other-task"))
    with pytest.raises(backup.Refused, match="not recorded"):
        world.registry.drill(world.backup, "0" * 64, 3, world.plan("unknown"))
    assert world.registry.path.read_bytes() == before


def test_a_same_attempt_rerun_on_a_slower_clock_returns_the_recorded_entry(world):
    set_id = world.capture()
    first = world.registry.drill(world.backup, set_id, 1, world.plan())
    slower = world.plan(clock=cases.Clock(0.0, 40.0))
    assert world.registry.drill(world.backup, set_id, 1, slower) == first


def test_a_same_attempt_rerun_returns_the_recorded_entry(world):
    set_id = world.capture()
    first = world.registry.drill(world.backup, set_id, 1, world.plan())
    before = world.registry.path.read_bytes()
    assert world.registry.drill(world.backup, set_id, 1, world.plan()) == first
    assert world.registry.path.read_bytes() == before


def test_encoded_documents_are_key_ordered():
    assert backup._encoded({"b": 1, "a": 2}) == b'{"a": 2, "b": 1}'


def test_write_stages_its_temporary_file_beside_the_target(tmp_path):
    target = tmp_path / "folder" / "file"
    target.parent.mkdir()
    with mock.patch.object(backup.tempfile, "mkstemp", wraps=tempfile.mkstemp) as staged:
        backup._write(target, b"data")
    staged.assert_called_once_with(dir=target.parent, prefix=backup.PARTIAL_WRITE)
    assert target.read_bytes() == b"data"
    assert sorted(path.name for path in target.parent.iterdir()) == ["file"]


def test_a_failed_write_removes_its_temporary_file_and_raises_the_cause(tmp_path):
    target = tmp_path / "folder" / "file"
    target.parent.mkdir()
    with mock.patch.object(backup.os, "fsync", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            backup._write(target, b"data")
    assert list(target.parent.iterdir()) == []


def test_restore_refuses_a_component_this_release_lists_no_versions_for(world):
    set_id, target = world.capture(), world.root / "restore"
    access = dataclasses.replace(world.access(), versions={})
    with pytest.raises(backup.Refused) as refused:
        backup.restore(world.backup, set_id, target, access)
    assert str(refused.value) == backup.OLD_VERSION
    assert not target.exists()


def test_an_empty_registry_allows_no_retention(tmp_path):
    assert backup.Registry(tmp_path / "registry.json").retention_allowed() is False


def test_a_refused_query_is_unanswered_even_when_an_empty_answer_matches_capture(world):
    sources = dataclasses.replace(world.sources(), query=lambda catalog, store: "")
    set_id = backup.capture(sources, world.backup, world.doc["clock"]["captured"])

    def refused(catalog, store):
        raise archive.EvidenceRefused("withheld")

    report = backup.drill(world.backup, set_id, world.plan(query=refused))
    assert report["query_answered"] is False
    assert report[backup.METRIC] == backup.FAILED


@pytest.mark.parametrize("make", [False, True])
def test_restore_creates_a_nested_root_or_uses_an_existing_empty_one(world, make):
    set_id, target = world.capture(), world.root / "nested" / "deeper" / "restore"
    if make:
        target.mkdir(parents=True)
    backup.restore(world.backup, set_id, target, world.access()).catalog.close()
    assert json.loads((target / backup.MARKER).read_text()) == {"set_id": set_id}


def test_a_registry_under_a_missing_folder_records_its_first_set(tmp_path):
    registry = backup.Registry(tmp_path / "a" / "b" / "registry.json")
    entry = registry.record("0" * 64, 5.0)
    expected = {"status": backup.UPLOADED, "captured_at": 5.0, "attempt": 0, "report": None, "accepted_attempt": None}
    assert entry == expected
    assert json.loads(registry.path.read_text()) == {"0" * 64: expected}


def test_an_unknown_set_is_not_accepted_and_statuses_are_counted(world):
    world.capture()
    world.capture("later_capture")
    assert world.registry.accepted("0" * 64) is False
    assert world.registry.status()[backup.METRIC][backup.UPLOADED] == 2


def test_a_bounded_rebuild_reports_its_pending_backlog(world):
    set_id = world.capture()
    limited = dataclasses.replace(world.projections(), limits=index_worker.Limits(1, 64000, 1))
    report = backup.drill(world.backup, set_id, world.plan(projections=limited))
    assert report["rebuilt_projections"]["state"] == "bounded"
    assert report["rebuilt_projections"]["pending"] > 0


@pytest.mark.parametrize("case", ["A", "B", "C"])
def test_package_cases_match_committed_evidence(tmp_path, case):
    observed = json.loads(json.dumps(cases.run_case(case, tmp_path)))
    path = EVIDENCE / f"{case.lower()}-result.json"
    expected = json.loads(path.read_text()) if path.exists() else None
    assert observed == expected, json.dumps(observed, sort_keys=True)


def test_package_record_and_manifest_match_the_fixture_inputs():
    record = json.loads((EVIDENCE / "result.json").read_text())
    found = json.loads((EVIDENCE / "manifest.json").read_text())
    inputs = (cases.FIXTURE, cases.CASES_MODULE)
    assert found["input_sha256"] == {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs}
    assert found["fixture_inputs"] == record["fixture_inputs"] == cases.FIXTURE_INPUTS
    assert found["tested_commit"] == record["tested_commit"]
    assert record["evidence_class"] == cases.EVIDENCE_CLASS
