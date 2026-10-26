import copy
import hashlib
import json
import shutil
from collections import Counter
from pathlib import Path

import pytest

from scripts.swarm_v2 import contracts
from scripts.swarm_v2.artifacts import base, local
from scripts.swarm_v2.auth_context import Registration
from scripts.swarm_v2.checkpoints import model
from tests.sv2_ckp02_cases import scratch

pytestmark = pytest.mark.unit

ROOT = Path(__file__).parent
FIXTURES = ROOT / "fixtures" / "swarm_v2" / "checkpoints"
LEGACY = ROOT / "contracts" / "swarm_v2" / "fixtures" / "checkpoint" / "current.json"
INPUTS = ("native-resume.json", "context-only.json", "path-only-snapshot.json")
EVIDENCE = ROOT.parent / "evidence" / "SV2-CKP-01"
EVIDENCE_CLASS = (
    "mocked: a local durable directory backend behind a granted artifact store and fixture launch registrations; "
    "no object store, worker Pod or harness was used"
)


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _store(root: Path, task: str = "t1", execution: str = "e1", generation: int = 2) -> base.ArtifactStore:
    granted = Registration(
        grant_id="lgr-" + "1" * 32,
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
    return base.ArtifactStore(local.LocalBackend(root), lambda token: granted, "grant")


def _upload_from_worker(store: base.ArtifactStore, bundle: dict, worker: Path, skip: tuple[str, ...] = ()) -> None:
    worker.mkdir(parents=True, exist_ok=True)
    for role, text in bundle["materials"].items():
        (worker / role).write_text(text)
    for role in bundle["materials"]:
        if role not in skip:
            store.put(f"{bundle['manifest']['checkpoint_id']}.{role}", (worker / role).read_bytes())


def _keys(store: base.ArtifactStore) -> list[str]:
    return store.backend.keys("")


def _decisions(*validators: model.Validator) -> dict[str, int]:
    total: Counter[str] = Counter()
    for validator in validators:
        total.update(validator.metrics()[model.METRIC])
    return dict(sorted(total.items()))


def _shown(verdict: model.Verdict) -> dict:
    return {
        "completeness": verdict.completeness,
        "continuation": verdict.continuation,
        "reason": verdict.reason,
        "limitations": list(verdict.limitations),
        "authoritative": verdict.authoritative,
    }


def _positive_fixture(root: Path) -> dict:
    observed = {}
    for name, checkpoint in (("native", "ckpt-native-0001"), ("context", "ckpt-context-0001")):
        bundle = _fixture("native-resume.json" if name == "native" else "context-only.json")
        durable, worker = root / name / "durable", root / name / "worker"
        writer = model.Validator(_store(durable))
        _upload_from_worker(writer.store, bundle, worker)
        committed = writer.commit(bundle["manifest"])
        shutil.rmtree(worker)
        reader = model.Validator(_store(durable, execution="e2", generation=3))
        recovered = reader.verify(checkpoint)
        observed[name] = {
            "committed": _shown(committed),
            "verified_by_next_attempt_after_worker_removed": _shown(recovered),
            "worker_files_left": worker.exists(),
            "durable_objects": len(_keys(writer.store)),
            "checkpoint_completeness_by_reason": _decisions(writer, reader),
        }
    return observed


def _positive() -> tuple[dict, bool]:
    with scratch() as (one, two):
        observed = {"fixture": _positive_fixture(one), "second_fixture": _positive_fixture(two)}
    first = observed["fixture"]
    checks = [
        observed["fixture"] == observed["second_fixture"],
        first["native"]["verified_by_next_attempt_after_worker_removed"]["continuation"] == model.NATIVE,
        first["native"]["verified_by_next_attempt_after_worker_removed"]["authoritative"],
        first["context"]["verified_by_next_attempt_after_worker_removed"]["continuation"] == model.CONTEXT,
        first["context"]["verified_by_next_attempt_after_worker_removed"]["authoritative"],
        not first["native"]["worker_files_left"],
    ]
    return observed, all(checks)


def _rejection_fixture(root: Path) -> dict:
    snapshot = _fixture("path-only-snapshot.json")
    native = _fixture("native-resume.json")
    validator = model.Validator(_store(root / "durable"))
    _upload_from_worker(validator.store, native, root / "worker")
    before = _keys(validator.store)
    path_only = validator.commit(snapshot)
    alone = copy.deepcopy(native["manifest"])
    for name in ("inventory", "continuation", "branch_state", "reconstruction"):
        del alone[name]
    alone["artifact_hashes"] = {}
    conversation_and_path = validator.commit(alone)
    stale = model.Validator(_store(root / "durable", generation=1)).commit(native["manifest"])
    after_rejections = _keys(validator.store)
    text = json.dumps([_shown(path_only), _shown(conversation_and_path), _shown(stale)])
    corrected = validator.commit(native["manifest"])
    return {
        "path_only_snapshot": _shown(path_only),
        "manifest_without_materials": _shown(conversation_and_path),
        "stale_generation_commit": _shown(stale),
        "durable_keys_unchanged_by_rejections": before == after_rejections,
        "diagnostics_name_the_old_path": snapshot["worktrees"]["engineer-fixture-0001"] in text,
        "diagnostics_name_the_conversation": snapshot["conversation_id"] in text,
        "new_valid_request": _shown(corrected),
        "checkpoint_completeness_by_reason": validator.metrics()[model.METRIC],
    }


def _rejection() -> tuple[dict, bool]:
    with scratch() as (one, two):
        observed = {"fixture": _rejection_fixture(one), "second_fixture": _rejection_fixture(two)}
    first = observed["fixture"]
    checks = [
        observed["fixture"] == observed["second_fixture"],
        first["path_only_snapshot"]["completeness"] == model.DIAGNOSTIC,
        first["manifest_without_materials"]["completeness"] == model.INCOMPLETE,
        first["stale_generation_commit"]["reason"] == "scope_mismatch",
        first["durable_keys_unchanged_by_rejections"],
        not first["diagnostics_name_the_old_path"],
        not first["diagnostics_name_the_conversation"],
        first["new_valid_request"]["authoritative"],
    ]
    return observed, all(checks)


def _recovery_fixture(root: Path) -> dict:
    native = _fixture("native-resume.json")
    context = _fixture("context-only.json")
    durable = root / "durable"
    interrupted = model.Validator(_store(durable))
    _upload_from_worker(interrupted.store, native, root / "worker", skip=("native_session",))
    partial = interrupted.commit(native["manifest"])
    manifest_written = interrupted.store.recorded("ckpt-native-0001") is not None
    restarted = model.Validator(_store(durable))
    _upload_from_worker(restarted.store, native, root / "worker")
    resumed = restarted.commit(native["manifest"])
    keys = _keys(restarted.store)
    replayed = restarted.commit(native["manifest"])
    replay_added = sorted(set(_keys(restarted.store)) - set(keys))
    stale = model.Validator(_store(durable, generation=1)).commit(native["manifest"])
    stale_added = sorted(set(_keys(restarted.store)) - set(keys))
    legacy = json.loads(LEGACY.read_text())
    older = model.Validator(_store(root / "legacy", "implement-session-ingestion", "exe-synthetic-0007", 3))
    older.store.put(legacy["checkpoint_id"], json.dumps(legacy).encode())
    future = copy.deepcopy(context["manifest"])
    future["schema_version"] = "3.0"
    unsupported = restarted.commit(future)
    rolled = contracts.load()
    rolled["compat"]["contracts"]["checkpoint"]["write_minor"] = 1
    rollback = model.Validator(_store(durable), rolled)
    _upload_from_worker(rollback.store, context, root / "context-worker")
    before_rollback = _keys(rollback.store)
    return {
        "interrupted_upload": _shown(partial),
        "manifest_written_before_materials": manifest_written,
        "after_restart": _shown(resumed),
        "replay": _shown(replayed),
        "replay_added_keys": replay_added,
        "stale_attempt_after_commit": _shown(stale),
        "stale_attempt_added_keys": stale_added,
        "older_minor_manifest": _shown(older.verify(legacy["checkpoint_id"])),
        "unsupported_version": _shown(unsupported),
        "previous_checkpoint_after_unsupported_version": _shown(restarted.verify("ckpt-native-0001")),
        "rollback_new_promotion": _shown(rollback.commit(context["manifest"])),
        "rollback_durable_keys_unchanged": before_rollback == _keys(rollback.store),
        "rollback_reads_committed_checkpoint": _shown(rollback.verify("ckpt-native-0001")),
        "checkpoint_completeness_by_reason": _decisions(interrupted, restarted, older, rollback),
    }


def _recovery() -> tuple[dict, bool]:
    with scratch() as (one, two):
        observed = {"fixture": _recovery_fixture(one), "second_fixture": _recovery_fixture(two)}
    first = observed["fixture"]
    checks = [
        observed["fixture"] == observed["second_fixture"],
        first["interrupted_upload"]["reason"] == "missing_object",
        not first["manifest_written_before_materials"],
        first["after_restart"]["authoritative"],
        first["replay"] == first["after_restart"],
        first["replay_added_keys"] == [],
        first["stale_attempt_after_commit"]["reason"] == "scope_mismatch",
        first["stale_attempt_added_keys"] == [],
        first["older_minor_manifest"]["limitations"] == list(model.LEGACY_LIMITS),
        not first["older_minor_manifest"]["authoritative"],
        first["unsupported_version"]["reason"] == "unsupported_version",
        first["previous_checkpoint_after_unsupported_version"]["authoritative"],
        first["rollback_new_promotion"]["reason"] == "promotion_disabled",
        first["rollback_durable_keys_unchanged"],
        first["rollback_reads_committed_checkpoint"]["authoritative"],
    ]
    return observed, all(checks)


def run_case(case: str) -> dict:
    observed, passed = {"a": _positive, "b": _rejection, "c": _recovery}[case]()
    return {
        "case": f"T-SV2-CKP-01-{case.upper()}",
        "state": "passed" if passed else "failed",
        "evidence_class": EVIDENCE_CLASS,
        "input_sha256": {name: hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() for name in INPUTS},
        "observed": observed,
    }


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    first, second = run_case(case), run_case(case)
    assert first == second
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True)
    path = EVIDENCE / f"{case}-result.json"
    committed = json.loads(path.read_text()) if path.exists() else None
    assert committed == first, json.dumps(first, indent=2, sort_keys=True)
