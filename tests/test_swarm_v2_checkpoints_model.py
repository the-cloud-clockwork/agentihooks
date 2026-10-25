import copy
import json
from pathlib import Path

import pytest

from scripts.swarm_v2 import contracts
from scripts.swarm_v2.artifacts import base, local
from scripts.swarm_v2.auth_context import Registration
from scripts.swarm_v2.checkpoints import model

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures" / "swarm_v2" / "checkpoints"
LEGACY = Path(__file__).parent / "contracts" / "swarm_v2" / "fixtures" / "checkpoint"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def registration(task: str = "t1", execution: str = "e1", generation: int = 2) -> Registration:
    return Registration(
        grant_id="lgr-" + "0" * 32,
        swarm_id="s1",
        execution_id=execution,
        generation=generation,
        seat_id="eng-1",
        task_id=task,
        account="acct",
        brain_id="brain",
        project_ids=["local:fixture"],
        issuer="issuer",
        audience="audience",
        key_id="key",
        registered_at="2026-10-11T00:00:00Z",
    )


def store(root: Path, **grant) -> base.ArtifactStore:
    return base.ArtifactStore(local.LocalBackend(root / "durable"), lambda token: registration(**grant), "grant")


def upload(target: base.ArtifactStore, bundle: dict, skip: tuple[str, ...] = ()) -> None:
    for role, text in bundle["materials"].items():
        if role not in skip:
            target.put(f"{bundle['manifest']['checkpoint_id']}.{role}", text.encode())


def keys(target: base.ArtifactStore) -> list[str]:
    return target.backend.keys("")


@pytest.fixture
def native():
    return fixture("native-resume.json")


@pytest.fixture
def context():
    return fixture("context-only.json")


def verdict(completeness, continuation, reason, limitations=(), authoritative=False):
    return model.Verdict(completeness, continuation, reason, tuple(limitations), authoritative)


def test_fixture_inventories_name_the_bytes_their_materials_hold(native, context):
    for bundle in (native, context):
        for role, text in bundle["materials"].items():
            ref = base.ArtifactRef.of(text.encode())
            assert bundle["manifest"]["inventory"][role] == {"sha256": ref.sha256, "size": ref.size}


def test_a_native_bundle_commits_last_and_verifies_as_native_resume(native, tmp_path):
    target = store(tmp_path)
    upload(target, native)
    validator = model.Validator(target)
    committed = validator.commit(native["manifest"])
    assert committed == verdict(model.COMPLETE, model.NATIVE, model.NATIVE, authoritative=True)
    assert validator.verify("ckpt-native-0001") == committed
    stored = target.recorded("ckpt-native-0001")
    assert json.loads(target.get_range(stored)) == native["manifest"]
    assert validator.metrics() == {model.METRIC: {"complete/native": 2}}


def test_a_context_bundle_is_explicitly_context_only(context, tmp_path):
    target = store(tmp_path)
    upload(target, context)
    validator = model.Validator(target)
    expected = verdict(model.COMPLETE, model.CONTEXT, model.CONTEXT, model.CONTEXT_LIMITS, True)
    assert validator.commit(context["manifest"]) == expected
    assert validator.verify("ckpt-context-0001") == expected


def test_classify_alone_never_grants_authority(native):
    validator = model.Validator(store(Path("/nonexistent-never-written")))
    assert validator.classify(native["manifest"]) == verdict(model.COMPLETE, model.NATIVE, model.NATIVE)


def test_a_path_only_snapshot_is_diagnostic_and_writes_nothing(tmp_path):
    snapshot = fixture("path-only-snapshot.json")
    target = store(tmp_path)
    validator = model.Validator(target)
    expected = verdict(model.DIAGNOSTIC, model.NONE, "path_only", model.PATH_ONLY_LIMITS)
    assert validator.classify(snapshot) == expected
    assert validator.commit(snapshot) == expected
    assert keys(target) == []
    shown = json.dumps(list(expected.limitations) + [expected.reason])
    assert "worktrees/agentihooks" not in shown
    assert snapshot["conversation_id"] not in shown


@pytest.mark.parametrize("name", ["current.json", "previous-minor.json"])
def test_an_older_minor_keeps_its_limitations_and_is_never_promoted(name, tmp_path):
    legacy = json.loads((LEGACY / name).read_text())
    target = store(tmp_path, execution="exe-synthetic-0007", task="implement-session-ingestion", generation=3)
    validator = model.Validator(target)
    expected = verdict(model.DIAGNOSTIC, model.NONE, "legacy_minor", model.LEGACY_LIMITS)
    assert validator.commit(legacy) == expected
    assert keys(target) == []
    target.put(legacy["checkpoint_id"], json.dumps(legacy).encode())
    assert validator.verify(legacy["checkpoint_id"]) == expected


@pytest.mark.parametrize(
    ("version", "reason"),
    [("3.0", "unsupported_version"), (None, "unsupported_version"), ("two", "unsupported_version")],
)
def test_an_unsupported_version_is_refused_without_writes(native, tmp_path, version, reason):
    doc = copy.deepcopy(native["manifest"])
    if version is None:
        del doc["schema_version"]
    else:
        doc["schema_version"] = version
    target = store(tmp_path)
    validator = model.Validator(target)
    assert validator.commit(doc) == verdict(model.INCOMPLETE, model.NONE, reason)
    assert keys(target) == []


def test_a_record_that_fails_its_schema_is_invalid(native):
    doc = copy.deepcopy(native["manifest"])
    doc["inventory"]["native_session"]["size"] = -1
    validator = model.Validator(store(Path("/nonexistent-never-written")))
    assert validator.classify(doc) == verdict(model.INCOMPLETE, model.NONE, "invalid_record")


def test_a_non_object_is_invalid():
    validator = model.Validator(store(Path("/nonexistent-never-written")))
    assert validator.classify(["not", "a", "manifest"]) == verdict(model.INCOMPLETE, model.NONE, "invalid_record")


def mutate(doc: dict, change) -> dict:
    doc = copy.deepcopy(doc)
    change(doc)
    return doc


def _drop(*path):
    def change(doc):
        node = doc
        for part in path[:-1]:
            node = node[part]
        del node[path[-1]]

    return change


def _set(value, *path):
    def change(doc):
        node = doc
        for part in path[:-1]:
            node = node[part]
        node[path[-1]] = value

    return change


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (_set("preparing", "status"), "not_committed"),
        (_set("uploading", "status"), "not_committed"),
        (_set(None, "committed_at"), "not_committed"),
        (_drop("continuation"), "missing_material"),
        (_drop("branch_state"), "missing_material"),
        (_drop("inventory"), "missing_material"),
        (_drop("reconstruction"), "missing_material"),
        (_drop("inventory", "native_session"), "missing_material"),
        (_drop("inventory", "normalized_transcript"), "missing_material"),
        (_drop("inventory", "source_bundle"), "missing_material"),
        (_set("unverified", "native_resume_capability"), "native_unqualified"),
        (_set("unsupported", "native_resume_capability"), "native_unqualified"),
        (_drop("native_session_id"), "native_unqualified"),
        (_set(None, "native_session_id"), "native_unqualified"),
        (_drop("native_harness_version"), "native_unqualified"),
        (_set("partial_capture", "quiescence_method"), "inconsistent_capture"),
        (_drop("transcript_durable_cursor", "sequence"), "missing_watermark"),
        (_drop("transcript_durable_cursor", "byte_offset"), "missing_watermark"),
        (_set(-1, "transcript_durable_cursor", "sequence"), "missing_watermark"),
        (_set(-1, "transcript_durable_cursor", "byte_offset"), "missing_watermark"),
        (_set(True, "transcript_durable_cursor", "sequence"), "missing_watermark"),
        (_set("41", "transcript_durable_cursor", "byte_offset"), "missing_watermark"),
        (_set("sha256:" + "0" * 64, "artifact_hashes", "native_session"), "inventory_mismatch"),
        (_drop("artifact_hashes", "native_session"), "inventory_mismatch"),
        (_set(3, "reconstruction", "execution_generation"), "scope_mismatch"),
        (_set(1, "reconstruction", "execution_generation"), "scope_mismatch"),
    ],
)
def test_a_native_manifest_lacking_a_requirement_is_incomplete(native, change, reason):
    validator = model.Validator(store(Path("/nonexistent-never-written")))
    assert validator.classify(mutate(native["manifest"], change)) == verdict(model.INCOMPLETE, model.NONE, reason)


@pytest.mark.parametrize("method", ["harness_snapshot", "stopped_process", "consistent_export"])
def test_every_consistent_capture_qualifies_native_resume(native, method):
    validator = model.Validator(store(Path("/nonexistent-never-written")))
    doc = mutate(native["manifest"], _set(method, "quiescence_method"))
    assert validator.classify(doc) == verdict(model.COMPLETE, model.NATIVE, model.NATIVE)


def test_a_watermark_at_the_first_record_is_complete(native):
    validator = model.Validator(store(Path("/nonexistent-never-written")))
    doc = mutate(native["manifest"], _set({"sequence": 0, "byte_offset": 0}, "transcript_durable_cursor"))
    assert validator.classify(doc) == verdict(model.COMPLETE, model.NATIVE, model.NATIVE)


def test_a_context_manifest_needs_no_native_material_or_consistent_capture(context):
    validator = model.Validator(store(Path("/nonexistent-never-written")))
    assert context["manifest"]["quiescence_method"] == "partial_capture"
    assert "native_session_id" not in context["manifest"]
    expected = verdict(model.COMPLETE, model.CONTEXT, model.CONTEXT, model.CONTEXT_LIMITS)
    assert validator.classify(context["manifest"]) == expected
    bundled = mutate(context["manifest"], _set("bundled", "branch_state"))
    assert validator.classify(bundled) == verdict(model.INCOMPLETE, model.NONE, "missing_material")


def test_an_interrupted_upload_never_writes_the_manifest_and_replays_once_complete(native, tmp_path):
    target = store(tmp_path)
    upload(target, native, skip=("native_session",))
    validator = model.Validator(target)
    assert validator.commit(native["manifest"]) == verdict(model.INCOMPLETE, model.NONE, "missing_object")
    assert target.recorded("ckpt-native-0001") is None
    assert validator.verify("ckpt-native-0001") == verdict(model.INCOMPLETE, model.NONE, "manifest_absent")
    upload(target, native)
    done = validator.commit(native["manifest"])
    assert done.authoritative
    before = keys(target)
    assert validator.commit(native["manifest"]) == done
    assert keys(target) == before


def test_a_corrupt_object_blocks_commit_and_verification(native, tmp_path):
    target = store(tmp_path)
    upload(target, native)
    validator = model.Validator(target)
    assert validator.commit(native["manifest"]).authoritative
    sha = native["manifest"]["inventory"]["native_session"]["sha256"]
    path = tmp_path / "durable" / target.scope.key("objects", sha)
    path.write_bytes(b"x" * path.stat().st_size)
    expected = verdict(model.INCOMPLETE, model.NONE, "corrupt_object")
    assert validator.verify("ckpt-native-0001") == expected
    assert validator.commit(native["manifest"]) == expected


def test_a_corrupt_or_unreadable_manifest_is_never_authoritative(native, tmp_path):
    target = store(tmp_path)
    upload(target, native)
    validator = model.Validator(target)
    validator.commit(native["manifest"])
    ref = target.recorded("ckpt-native-0001")
    path = tmp_path / "durable" / target.scope.key("objects", ref.sha256)
    path.write_bytes(b"{" * ref.size)
    assert validator.verify("ckpt-native-0001") == verdict(model.INCOMPLETE, model.NONE, "corrupt_manifest")
    target.put("not-json", b"\xff not json")
    assert validator.verify("not-json") == verdict(model.INCOMPLETE, model.NONE, "corrupt_manifest")


def test_a_manifest_stored_under_another_identity_is_refused(native, tmp_path):
    target = store(tmp_path)
    upload(target, native)
    target.put("ckpt-other-0002", json.dumps(native["manifest"]).encode())
    validator = model.Validator(target)
    assert validator.verify("ckpt-other-0002") == verdict(model.INCOMPLETE, model.NONE, "identity_mismatch")


def test_a_committed_identity_cannot_be_rewritten(native, tmp_path):
    target = store(tmp_path)
    upload(target, native)
    validator = model.Validator(target)
    first = validator.commit(native["manifest"])
    before = keys(target)
    rewritten = mutate(native["manifest"], _set(2, "checkpoint_sequence"))
    assert validator.commit(rewritten) == verdict(model.INCOMPLETE, model.NONE, "identity_conflict")
    assert keys(target) == before
    assert validator.verify("ckpt-native-0001") == first


def test_an_equal_manifest_in_another_key_order_replays_the_commit(native, tmp_path):
    target = store(tmp_path)
    upload(target, native)
    validator = model.Validator(target)
    first = validator.commit(native["manifest"])
    before = keys(target)
    reordered = dict(reversed(list(native["manifest"].items())))
    assert list(reordered) != list(native["manifest"])
    assert validator.commit(reordered) == first
    assert keys(target) == before


@pytest.mark.parametrize(
    "grant",
    [
        {"task": "t9"},
        {"execution": "e2"},
        {"generation": 3},
        {"generation": 1},
    ],
)
def test_only_the_current_attempt_may_commit_its_checkpoint(native, tmp_path, grant):
    target = store(tmp_path, **grant)
    upload(target, native)
    before = keys(target)
    validator = model.Validator(target)
    assert validator.commit(native["manifest"]) == verdict(model.INCOMPLETE, model.NONE, "scope_mismatch")
    assert keys(target) == before


def test_a_newer_attempt_of_the_same_task_verifies_an_earlier_checkpoint(native, tmp_path):
    writer = store(tmp_path)
    upload(writer, native)
    model.Validator(writer).commit(native["manifest"])
    reader = model.Validator(store(tmp_path, execution="e2", generation=3))
    assert reader.verify("ckpt-native-0001") == verdict(model.COMPLETE, model.NATIVE, model.NATIVE, (), True)


def test_distinct_task_and_execution_generations_validate_against_their_own_sources(tmp_path, native):
    target = store(tmp_path, generation=7)
    doc = native["manifest"]
    doc["authority"]["task_generation"] = 7
    doc["authority"]["execution_generation"] = 2
    doc["reconstruction"]["execution_generation"] = 2
    upload(target, native)
    validator = model.Validator(target, execution_generation=2)
    assert validator.commit(doc).authoritative
    assert validator.verify(doc["checkpoint_id"]).authoritative
    mismatched = model.Validator(target, execution_generation=3)
    assert mismatched.verify(doc["checkpoint_id"]).reason == "scope_mismatch"


def test_an_old_record_keeps_its_generation_compatibility(tmp_path, native):
    target = store(tmp_path)
    upload(target, native)
    validator = model.Validator(target)
    assert "execution_generation" not in native["manifest"]["authority"]
    assert validator.commit(native["manifest"]).authoritative
    assert model.Validator(target, execution_generation=9).verify(native["manifest"]["checkpoint_id"]).authoritative


def test_another_task_cannot_see_the_checkpoint(native, tmp_path):
    writer = store(tmp_path)
    upload(writer, native)
    model.Validator(writer).commit(native["manifest"])
    reader = model.Validator(store(tmp_path, task="t9", generation=3))
    assert reader.verify("ckpt-native-0001") == verdict(model.INCOMPLETE, model.NONE, "manifest_absent")


@pytest.mark.parametrize(
    "change",
    [_set("t9", "authority", "task_id"), _set(3, "authority", "task_generation")],
)
def test_a_stored_manifest_outside_the_reader_scope_is_refused(native, tmp_path, change):
    target = store(tmp_path)
    upload(target, native)
    doc = mutate(native["manifest"], change)
    doc["reconstruction"]["execution_generation"] = doc["authority"]["task_generation"]
    target.put("ckpt-native-0001", json.dumps(doc).encode())
    validator = model.Validator(target)
    assert validator.verify("ckpt-native-0001") == verdict(model.INCOMPLETE, model.NONE, "scope_mismatch")


def test_rollback_disables_promotion_and_keeps_reading_committed_checkpoints(native, context, tmp_path):
    target = store(tmp_path)
    upload(target, native)
    upload(target, context)
    model.Validator(target).commit(native["manifest"])
    rolled = contracts.load()
    rolled["compat"]["contracts"]["checkpoint"]["write_minor"] = 1
    validator = model.Validator(target, rolled)
    before = keys(target)
    assert validator.commit(context["manifest"]) == verdict(model.INCOMPLETE, model.NONE, "promotion_disabled")
    assert keys(target) == before
    assert validator.verify("ckpt-native-0001").authoritative


def test_the_default_contracts_write_the_typed_minor():
    assert contracts.write_version(contracts.load(), "checkpoint") == "2.2"


def test_metrics_count_each_decision_by_completeness_and_reason(native, tmp_path):
    target = store(tmp_path)
    validator = model.Validator(target)
    validator.classify(fixture("path-only-snapshot.json"))
    validator.commit(native["manifest"])
    validator.verify("ckpt-native-0001")
    upload(target, native)
    validator.commit(native["manifest"])
    assert validator.metrics() == {
        model.METRIC: {
            "diagnostic_only/path_only": 1,
            "incomplete/missing_object": 1,
            "incomplete/manifest_absent": 1,
            "complete/native": 1,
        }
    }
    assert model.Validator(target).metrics() == {model.METRIC: {}}


def test_a_snapshot_shape_is_path_only_only_without_a_schema_version(native):
    validator = model.Validator(store(Path("/nonexistent-never-written")))
    doc = mutate(native["manifest"], _set({"a": "/x"}, "worktrees"))
    assert validator.classify(doc) == verdict(model.COMPLETE, model.NATIVE, model.NATIVE)
    bare = {"conversation_id": "native-session-fixture-0001"}
    assert validator.classify(bare).reason == "path_only"
    assert validator.classify({"worktrees": {}}).reason == "path_only"
    assert validator.classify({"slug": "fixture"}).reason == "unsupported_version"
