"""Tests for SV2-RET-04 backup manifests and restore drills."""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from hooks.memory import archive
from scripts.swarm_v2 import backup
from tests import sv2_ret04_cases as cases

pytestmark = pytest.mark.unit
EVIDENCE = Path(__file__).parents[1] / "evidence" / "SV2-RET-04"


@pytest.fixture
def world(tmp_path):
    return cases.World(tmp_path)


def manifest(world, set_id):
    return json.loads(world.backup.get(set_id))


def test_capture_records_every_component_with_its_version_and_key_id(world):
    found = manifest(world, world.capture())
    assert found["format"] == backup.FORMAT
    assert found["captured_at"] == world.doc["clock"]["captured"]
    assert sorted(found["components"]) == sorted((backup.CATALOG, *backup.FILES))
    for name, component in found["components"].items():
        assert component["version"] == world.doc["versions"][name]
        assert component["key_id"] == world.doc["key_ids"][name]
        data = world.backup.get(component["digest"])
        assert component["bytes"] == len(data)
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
    assert "session_event" in tables and "archive_manifest" in tables
    assert found["omitted_projections"] == list(backup.PROJECTIONS)


def test_the_set_id_is_the_digest_of_its_manifest(world):
    set_id = world.capture()
    assert hashlib.sha256(world.backup.get(set_id)).hexdigest() == set_id


def test_restore_refuses_a_withheld_key_before_writing(world):
    set_id, target = world.capture(), world.root / "restore"
    with pytest.raises(backup.Refused, match=backup.NO_KEY):
        backup.restore(world.backup, set_id, target, world.keyring(world.doc["withheld_key"]))
    assert not target.exists()


def test_restore_refuses_another_major_format_before_writing(world):
    found = manifest(world, world.capture())
    future = world.backup.put(json.dumps({**found, "format": "2.0"}).encode())
    with pytest.raises(backup.Refused, match="major version"):
        backup.restore(world.backup, future, world.root / "restore", world.keyring())
    assert not (world.root / "restore").exists()


def test_restore_accepts_a_newer_minor_format(world):
    found = manifest(world, world.capture())
    newer = world.backup.put(json.dumps({**found, "format": "1.7"}).encode())
    restored = backup.restore(world.backup, newer, world.root / "restore", world.keyring())
    restored.catalog.close()
    assert restored.manifest["format"] == "1.7"


def test_restore_refuses_a_folder_holding_other_data(world):
    set_id, target = world.capture(), world.root / "occupied"
    target.mkdir()
    (target / "other").write_text("kept")
    with pytest.raises(backup.Refused, match="empty folder"):
        backup.restore(world.backup, set_id, target, world.keyring())
    assert sorted(path.name for path in target.iterdir()) == ["other"]


def test_restore_refuses_a_folder_claimed_by_another_set(world):
    first = world.capture()
    second = world.capture("later_capture")
    target = world.root / "restore"
    backup.restore(world.backup, first, target, world.keyring()).catalog.close()
    with pytest.raises(backup.Refused, match="empty folder"):
        backup.restore(world.backup, second, target, world.keyring())


def test_restore_resumes_into_its_own_folder_without_duplicates(world):
    set_id, target = world.capture(), world.root / "restore"
    with pytest.raises(cases.Interrupted):
        backup.restore(cases.Interrupting(world.backup.root, 5), set_id, target, world.keyring())
    assert not (target / backup.CATALOG_FILE).exists()
    restored = backup.restore(world.backup, set_id, target, world.keyring())
    restored.catalog.close()
    assert cases.object_files(target) == 5
    assert restored.irrecoverable == {world.lost[0]: archive.MISSING}


def test_drill_rebuilds_projections_answers_the_query_and_recovers_the_checkpoint(world):
    set_id = world.capture()
    report = backup.drill(world.backup, set_id, world.plan())
    assert report[backup.METRIC] == backup.PARTIAL
    assert report["query_answered"] is True
    assert report["checkpoint"] == {"task_id": "fixture-task", "objects": 2, "recovered": True}
    assert report["rebuilt_projections"] == {"sessions": 2, "state": "drained", "pending": 0}
    assert report["recovery_point"] == world.doc["clock"]["captured"]
    assert report["recovery_time_seconds"] == 2 * world.doc["clock"]["step"]
    assert world.restored_rows()[0] == world.source_rows()


def test_drill_is_verified_when_no_source_object_is_missing(world):
    lost = next(item for item in world.doc["sessions"] if "lost_batch" in item)
    data = b"".join(cases.line(record) for record in lost["turns"][lost["lost_batch"]])
    assert world.objects.put(data) == world.lost[0]
    report = backup.drill(world.backup, world.capture(), world.plan())
    assert report[backup.METRIC] == backup.VERIFIED
    assert report["irrecoverable"] == {}


def test_drill_fails_when_the_query_cannot_be_answered(world):
    set_id = world.capture()
    lost = next(item["session_uid"] for item in world.doc["sessions"] if "lost_batch" in item)
    world.doc["query_session"] = lost
    report = backup.drill(world.backup, set_id, world.plan())
    assert report[backup.METRIC] == backup.FAILED
    assert report["query_answered"] is False


def test_drill_fails_when_the_designated_checkpoint_is_unknown(world):
    set_id = world.capture()
    plan = world.plan()
    report = backup.drill(world.backup, set_id, backup.Drill(**{**plan.__dict__, "task_id": "other-task"}))
    assert report[backup.METRIC] == backup.FAILED
    assert report["checkpoint"] == {"task_id": "other-task", "objects": 0, "recovered": False}


def test_drill_fails_when_a_designated_checkpoint_object_is_missing(world):
    world.objects._path(world.checkpoint[1]).unlink()
    report = backup.drill(world.backup, world.capture(), world.plan())
    assert report[backup.METRIC] == backup.FAILED
    assert report["checkpoint"]["recovered"] is False
    assert report["irrecoverable"][world.checkpoint[1]] == archive.MISSING


def test_drill_fails_on_an_altered_backup_component(world):
    set_id = world.capture()
    digest = manifest(world, set_id)["components"]["brain"]["digest"]
    world.backup._path(digest).write_bytes(b"altered")
    report = backup.drill(world.backup, set_id, world.plan())
    assert report == {backup.METRIC: backup.FAILED, "reason": archive.ALTERED, "recovery_time_seconds": 2.5}


def test_an_upload_without_a_drill_is_not_accepted(world):
    set_id = world.capture()
    assert world.registry.accepted(set_id) is False
    assert world.registry.preserved() is None
    assert world.registry.retention_allowed() is False
    assert world.registry.status()[backup.METRIC][backup.UPLOADED] == 1


def test_record_replays_without_resetting_a_verified_set(world):
    set_id = world.capture()
    world.registry.verify(set_id, 1, {backup.METRIC: backup.VERIFIED})
    assert world.registry.record(set_id, 0.0)["status"] == backup.VERIFIED


@pytest.mark.parametrize(
    ("status", "accepted", "retention"),
    [(backup.VERIFIED, True, True), (backup.PARTIAL, True, False), (backup.FAILED, False, False)],
)
def test_acceptance_and_retention_follow_the_drill_status(world, status, accepted, retention):
    set_id = world.capture()
    world.registry.verify(set_id, 1, {backup.METRIC: status})
    assert world.registry.accepted(set_id) is accepted
    assert world.registry.retention_allowed() is retention
    assert (world.registry.preserved() == set_id) is accepted


def test_a_newer_unverified_set_stops_retention_and_keeps_the_verified_set(world):
    verified = world.capture()
    world.registry.verify(verified, 1, {backup.METRIC: backup.VERIFIED})
    newer = world.capture("later_capture")
    assert world.registry.retention_allowed() is False
    assert world.registry.preserved() == verified
    world.registry.verify(newer, 1, {backup.METRIC: backup.VERIFIED})
    assert world.registry.retention_allowed() is True
    assert world.registry.preserved() == newer


def test_a_stale_or_unknown_drill_result_changes_nothing(world):
    set_id = world.capture()
    world.registry.verify(set_id, 2, {backup.METRIC: backup.PARTIAL})
    before = world.registry.path.read_bytes()
    with pytest.raises(backup.Refused, match="newer restore drill"):
        world.registry.verify(set_id, 1, {backup.METRIC: backup.VERIFIED})
    with pytest.raises(backup.Refused, match="newer restore drill"):
        world.registry.verify(set_id, 2, {backup.METRIC: backup.VERIFIED})
    with pytest.raises(backup.Refused, match="not recorded"):
        world.registry.verify("0" * 64, 3, {backup.METRIC: backup.VERIFIED})
    assert world.registry.path.read_bytes() == before
    assert world.registry.verify(set_id, 2, {backup.METRIC: backup.PARTIAL})["attempt"] == 2


@pytest.mark.parametrize("case", ["A", "B", "C"])
def test_package_cases_match_committed_evidence(tmp_path, case):
    observed = json.loads(json.dumps(cases.run_case(case, tmp_path)))
    path = EVIDENCE / f"{case.lower()}-result.json"
    expected = json.loads(path.read_text()) if path.exists() else None
    assert observed == expected, json.dumps(observed, sort_keys=True)


def test_package_record_and_manifest_match_the_fixture_inputs():
    record = json.loads((EVIDENCE / "result.json").read_text())
    found = json.loads((EVIDENCE / "manifest.json").read_text())
    assert found["input_sha256"] == {cases.FIXTURE.name: hashlib.sha256(cases.FIXTURE.read_bytes()).hexdigest()}
    assert found["fixture_inputs"] == record["fixture_inputs"] == cases.FIXTURE_INPUTS
    assert found["tested_commit"] == record["tested_commit"]
    assert record["evidence_class"] == cases.EVIDENCE_CLASS
