"""Tests for SV2-RET-05 runtime and index garbage collection."""

import hashlib
import json
from pathlib import Path
from unittest import mock

import pytest

from hooks.memory import archive
from hooks.memory import catalog as catalog_module
from hooks.memory.catalog import Conflict
from scripts.swarm_v2 import garbage_collection as gc
from tests import sv2_ret05_cases as cases

pytestmark = pytest.mark.unit
EVIDENCE = Path(__file__).parents[1] / "evidence" / "SV2-RET-05"
UNKNOWN_UPLOAD = "no reservation is held under this upload id; nothing was committed"


@pytest.fixture
def world(tmp_path):
    found = cases.World(tmp_path)
    yield found
    found.catalog.close()


def test_collect_reclaims_the_abandoned_upload_and_keeps_committed_and_referenced_objects(world):
    report = world.collect()
    abandoned = world.uploads["abandoned"]
    assert report["reclaimed"]["uploads"] == sorted([abandoned, cases.digest(world.doc["never_landed"])])
    assert not world.store._path(abandoned).exists()
    assert world.readable(world.committed) and world.readable(world.pinned)
    assert set(report["protected"]["authoritative"]) == {*world.committed, world.uploads["checkpoint"]}
    assert report[gc.BYTES] == len(world.doc["uploads"]["abandoned"].encode())
    assert (report["mode"], report["committed"]) == ("collect", True)


def test_an_unexpired_reservation_protects_a_new_attempt(world):
    report = world.collect()
    assert report["protected"]["live"] == [world.uploads["live"]]
    assert world.store.get(world.uploads["live"]) == world.doc["uploads"]["live"].encode()


def test_grace_extends_an_expired_reservation(world):
    report = world.collect(at="early")
    assert report["reclaimed"]["uploads"] == []
    assert world.uploads["abandoned"] in report["protected"]["live"]
    assert world.store._path(world.uploads["abandoned"]).exists()


@pytest.mark.parametrize("grace", [0.0, 249.0])
def test_a_reservation_past_its_grace_is_collected(world, grace):
    plan = world.plan(at="early")
    plan = gc.Plan(plan.grant, plan.references, grace, plan.now)
    report = gc.collect(world.catalog, world.store, plan)
    assert world.uploads["abandoned"] in report["reclaimed"]["uploads"]


def test_a_lookalike_and_a_stray_file_without_reservation_are_rejected_and_kept(world):
    report = world.collect()
    assert report["protected"]["unowned"] == sorted([world.lookalike, "stray.tmp"])
    assert world.store.get(world.lookalike) == world.doc["lookalike"].encode()
    assert (world.store.root / "stray.tmp").read_text() == world.doc["stray"]
    assert report[gc.REJECTIONS] == 4


def test_another_domains_reservation_is_not_ownership_for_this_grant(world):
    report = world.collect()
    assert report["foreign_domain_objects"] == 1
    assert world.uploads["foreign"] not in json.dumps(report)
    assert world.store.get(world.uploads["foreign"]) == world.doc["uploads"]["foreign"].encode()


def test_objects_archived_before_reservations_existed_stay_authoritative_or_unowned(world):
    data, legacy = (
        b"an object archived by the older acknowledge path\n",
        b"an unreferenced object from the older path\n",
    )
    item = world.doc["session"]
    entry = catalog_module.ArchiveEntry(
        world.domain, item["session_uid"], item["source_generation"], 900, 900 + len(data), world.store.put(data)
    )
    assert archive.acknowledge(world.catalog, world.store, entry) is True
    orphan = world.store.put(legacy)
    report = world.collect()
    assert entry.object_digest in report["protected"]["authoritative"]
    assert orphan in report["protected"]["unowned"]
    assert world.store.get(orphan) == legacy


def test_bytes_that_do_not_hash_to_the_reserved_name_are_rejected_and_kept(world):
    report = world.collect()
    assert report["protected"]["altered"] == [world.altered]
    assert world.store._path(world.altered).read_text() == world.doc["altered"]["written"]


def test_a_digest_named_file_outside_its_shard_is_unowned(world):
    misplaced = world.store.root / world.uploads["abandoned"]
    misplaced.write_text("outside the shard")
    report = world.collect()
    assert world.uploads["abandoned"] in report["protected"]["unowned"]
    assert misplaced.read_text() == "outside the shard"


def test_inventory_reports_what_collect_would_reclaim_without_changing_state(world):
    world.retire()
    before = world.protected()
    inventory = world.collect(destructive=False)
    assert world.protected() == before
    assert (inventory["mode"], inventory["committed"]) == ("inventory", False)
    collected = world.collect()
    assert inventory["reclaimed"] == collected["reclaimed"]
    assert inventory[gc.BYTES] == collected[gc.BYTES]


def test_a_retired_generation_past_grace_is_dropped_and_the_active_one_kept(world):
    retired, active = world.doc["generations"]["retired"], world.doc["generations"]["active"]
    world.retire()
    vectors = dict(world.vectors())
    stored = world.catalog.db.execute(gc.INDEX_BYTES, (world.domain, retired)).fetchone()[0]
    report = world.collect()
    assert report["reclaimed"]["index_generations"] == [retired]
    assert dict(world.vectors()) == {active: vectors[active]}
    assert report[gc.BYTES] == stored + len(world.doc["uploads"]["abandoned"].encode())
    assert world.catalog.db.execute("SELECT * FROM index_retirement").fetchall() == []
    found = world.catalog.db.execute("SELECT index_generation FROM index_generation").fetchall()
    assert found == [(active,)]


def test_a_retired_generation_inside_its_grace_is_kept(world):
    world.retire()
    report = world.collect(at="early")
    assert report["protected"]["live"][-1] == world.doc["generations"]["retired"]
    assert len(world.vectors()) == 2


def test_work_queued_after_retirement_keeps_the_generation(world):
    world.retire()
    retired = world.doc["generations"]["retired"]
    row = (world.domain, retired, "chunk-new", "uid", "v", "hash", world.clock["retired"] + 1)
    world.catalog.db.execute("INSERT INTO embedding_work VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL)", row)
    report = world.collect()
    assert report["protected"]["index_in_use"] == [retired]
    assert len(world.vectors()) == 2


def test_work_queued_at_the_retirement_instant_does_not_keep_the_generation(world):
    world.retire()
    retired = world.doc["generations"]["retired"]
    row = (world.domain, retired, "chunk-new", "uid", "v", "hash", world.clock["retired"])
    world.catalog.db.execute("INSERT INTO embedding_work VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL)", row)
    assert world.collect()["reclaimed"]["index_generations"] == [retired]


def test_a_generation_activated_after_retirement_is_kept(world):
    world.retire()
    retired = world.doc["generations"]["retired"]
    world.catalog.db.execute("UPDATE index_cutover SET index_generation = ?", (retired,))
    report = world.collect()
    assert report["protected"]["active_index"] == [retired]


def test_retiring_the_active_generation_is_refused(world):
    assert cases.refused(lambda: world.retire("active")) == "the active index generation cannot be retired"
    assert world.catalog.db.execute("SELECT * FROM index_retirement").fetchall() == []


def test_retiring_an_unknown_generation_is_refused(world):
    world.doc["generations"]["missing"] = "fixture-embed-missing"
    with pytest.raises(Conflict):
        world.retire("missing")


def test_a_retirement_replay_keeps_the_first_time(world):
    first = world.retire()
    assert world.retire(at="collect") == first == world.clock["retired"]


def test_reserve_refuses_a_name_that_is_not_a_digest(world):
    with pytest.raises(ValueError, match="^an upload reservation must name a sha256 hex digest$"):
        world.reserve("bad", "abc", "attempt", 1.0)


def test_reserve_replays_and_refuses_a_conflicting_reuse_of_an_upload_id(world):
    target = world.uploads["abandoned"]
    expires = world.clock["expired"]
    assert world.reserve("upload-abandoned", target, "attempt-abandoned", expires)["upload_id"] == "upload-abandoned"
    conflict = cases.refused(lambda: world.reserve("upload-abandoned", target, "other", expires))
    assert conflict == "this upload id is already reserved for different bytes or another attempt"


def test_commit_releases_the_reservation_and_archives_the_entry(world):
    data = b"a late but valid upload\n"
    target = hashlib.sha256(data).hexdigest()
    world.reserve("late", target, "attempt-late", world.clock["expired"])
    world.store.put(data)
    item = world.doc["session"]
    entry = catalog_module.ArchiveEntry(world.domain, item["session_uid"], item["source_generation"], 900, 924, target)
    assert world.reservations.commit(cases.grant(world.doc), "late", world.store, entry) is True
    assert world.catalog.db.execute(gc.DIGEST_STATES, (target,)).fetchall() == []
    assert target in world.collect()["protected"]["authoritative"]


def test_commit_refuses_an_unknown_upload_and_another_domain(world):
    item = world.doc["session"]
    entry = catalog_module.ArchiveEntry(world.domain, item["session_uid"], item["source_generation"], 0, 1, "0" * 64)
    commit = world.reservations.commit
    assert cases.refused(lambda: commit(cases.grant(world.doc), "nope", world.store, entry)) == UNKNOWN_UPLOAD
    foreign = cases.grant(world.doc, world.doc["foreign_corpus"])
    assert cases.refused(lambda: commit(foreign, "upload-foreign", world.store, entry)) == (
        "the archive entry belongs to another access domain than the grant; nothing was committed"
    )


def test_commit_after_collection_finds_no_reservation_and_archives_nothing(world):
    world.collect()
    item = world.doc["session"]
    target = world.uploads["abandoned"]
    entry = catalog_module.ArchiveEntry(world.domain, item["session_uid"], item["source_generation"], 900, 950, target)
    commit = world.reservations.commit
    grant = cases.grant(world.doc)
    assert cases.refused(lambda: commit(grant, "upload-abandoned", world.store, entry)) == UNKNOWN_UPLOAD
    assert world.catalog.db.execute(gc.ARCHIVED, (target,)).fetchone() is None


def test_a_crash_after_claiming_resumes_and_keeps_a_newly_referenced_checkpoint(tmp_path):
    report = cases.recovery(tmp_path)
    target_state = report["claimed_before_rerun"]
    assert report["crashed"] is True
    assert [state for _, state in target_state] == [gc.COLLECTING]
    assert report["reserve_during_collection"] == (
        "this object is being collected; reserve it again after the collection finishes"
    )
    assert report["checkpoint_readable"] is True
    assert len(report["rerun"]["protected"]["rebound"]) == 1
    assert report["replay"]["reclaimed"] == {"uploads": [], "index_generations": []}
    assert report["reserve_after"] == "again"


def test_a_claim_rebound_between_claim_and_delete_keeps_the_bytes(world):
    target = world.uploads["abandoned"]
    claim = gc._claim

    def rebinding(catalog, plan, digest):
        found = claim(catalog, plan, digest)
        world.pinned.add(target)
        return found

    with mock.patch.object(gc, "_claim", rebinding):
        report = world.collect()
    assert target in report["protected"]["rebound"]
    assert world.store.get(target) == world.doc["uploads"]["abandoned"].encode()
    assert world.catalog.db.execute(gc.DIGEST_STATES, (target,)).fetchall() == []


def test_a_reservation_renewed_before_the_claim_keeps_the_object(world):
    target = world.uploads["abandoned"]
    intact = gc._intact

    def renewing(path, digest):
        if digest == target:
            world.reserve("renewed", target, "attempt-new", world.clock["live_until"])
        return intact(path, digest)

    with mock.patch.object(gc, "_intact", renewing):
        report = world.collect()
    assert target in report["protected"]["live"]
    assert world.store._path(target).exists()


def test_a_reference_found_at_the_claim_keeps_a_live_attempts_reservation(world):
    target = world.uploads["abandoned"]
    intact = gc._intact

    def referencing(path, digest):
        if digest == target:
            world.reserve("renewed", target, "attempt-new", world.clock["live_until"])
            world.pinned.add(target)
        return intact(path, digest)

    with mock.patch.object(gc, "_intact", referencing):
        report = world.collect()
    assert target in report["protected"]["authoritative"]
    assert world.store._path(target).exists()
    states = world.catalog.db.execute(gc.DIGEST_STATES, (target,)).fetchall()
    assert sorted(states) == [(world.domain, 1100.0, "reserved"), (world.domain, 5000.0, "reserved")]


def test_a_reference_to_bytes_a_crashed_run_already_deleted_is_reported_missing(world):
    target = world.uploads["abandoned"]
    world.catalog.db.execute(gc.CLAIM, (gc.COLLECTING, target))
    world.store._path(target).unlink()
    world.pinned.add(target)
    report = world.collect()
    assert report["protected"]["missing"] == [target]
    assert target not in report["protected"]["rebound"]
    assert report[gc.REJECTIONS] == 5
    assert world.catalog.db.execute(gc.DIGEST_STATES, (target,)).fetchall() == []


def test_a_missing_store_folder_collects_only_reservations(tmp_path):
    world = cases.World(tmp_path / "w")
    plan = world.plan()
    report = gc.collect(world.catalog, archive.ObjectStore(tmp_path / "absent"), plan)
    assert cases.digest(world.doc["never_landed"]) in report["reclaimed"]["uploads"]
    world.catalog.close()


def test_main_runs_inventory_collect_and_retire(world, tmp_path, capsys):
    path, objects = str(world.catalog_path), str(world.store.root)
    world.catalog.close()
    references = tmp_path / "references.json"
    references.write_text(json.dumps(sorted(world.pinned)))
    args = [path, world.domain, "--principal", "operator-fixture", "--objects", objects]
    args += ["--references", str(references), "--grace", "300"]
    assert gc.main(["inventory", *args], clock=lambda: world.clock["collect"]) == 0
    inventory = json.loads(capsys.readouterr().out)
    retired = world.doc["generations"]["retired"]
    retire = ["retire", path, world.domain, "--principal", "operator-fixture", "--generation", retired]
    assert gc.main(retire, clock=lambda: 1300.0) == 0
    assert json.loads(capsys.readouterr().out) == {"index_generation": retired, "retired_at": 1300.0}
    assert gc.main(["collect", *args], clock=lambda: world.clock["collect"]) == 0
    collected = json.loads(capsys.readouterr().out)
    assert inventory["mode"] == "inventory" and collected["mode"] == "collect"
    assert collected["reclaimed"]["index_generations"] == [retired]
    assert world.uploads["checkpoint"] in collected["protected"]["authoritative"]
    world.catalog = catalog_module.Catalog(path)


@pytest.mark.parametrize(
    "argv",
    [
        ["retire", "c", "d", "--principal", "p"],
        ["inventory", "c", "d", "--principal", "p"],
        ["collect", "c", "d", "--principal", "p", "--objects", "o", "--generation", "g"],
        ["collect", "c", "d", "--principal", "p", "--objects", "o"],
        ["retire", "c", "d", "--generation", "g"],
    ],
)
def test_main_refuses_mismatched_flags(argv):
    with pytest.raises(SystemExit) as raised:
        gc.main(argv)
    assert raised.value.code == 2


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
