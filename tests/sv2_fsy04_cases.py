import json
import tempfile
from pathlib import Path

from scripts.swarm_v2.artifacts import base, local, object_store
from tests.test_swarm_v2_artifacts import DATA, KINDS, OTHER, World, registration

DIMENSIONS = {"fixture": "sv2-fsy-04"}


def _contents(world: World) -> dict[str, bytes]:
    return {key: world.backend.read(key, 0, world.backend.size(key)) for key in world.backend.keys("")}


def _restarted(world: World, root: Path) -> base.ArtifactStore:
    if world.kind == "local":
        return base.ArtifactStore(local.LocalBackend(root / "durable"))
    return base.ArtifactStore(object_store.ObjectStoreBackend(world.fake, "bucket", "swarm/"))


def _positive(kind: str) -> dict:
    with tempfile.TemporaryDirectory() as root:
        world = World(kind, Path(root))
        attempt = Path(root) / "attempt"
        attempt.mkdir()
        (attempt / "output.bin").write_bytes(DATA)
        ref = world.store.put(world.scope, "a1", (attempt / "output.bin").read_bytes())
        reader = _restarted(world, Path(root))
        manifest = world.store.commit_manifest(world.scope, "outputs", ["a1"])
        run = {
            "backend": kind,
            "reference_is_content_digest": ref == base.ArtifactRef.of(DATA),
            "verified_by_fresh_reader": reader.stat(world.scope, ref) == base.VERIFIED,
            "retrieved_by_reference": reader.get_range(world.scope, ref) == DATA,
            "manifest_lists_reference": manifest["artifacts"] == {"a1": {"sha256": ref.sha256, "size": ref.size}},
            "durable_keys_scoped_to_task": all(key.startswith("s1/t1/") for key in world.backend.keys("")),
            "attempt_files_untouched": [p.name for p in attempt.iterdir()] == ["output.bin"],
            "no_staging_left": not [key for key in world.backend.keys("") if "/staging/" in key],
        }
        run["passed"] = all(run[name] for name in run if name != "backend")
        run[base.METRIC] = world.store.metrics()[base.METRIC]
        return run


def case_a():
    runs = [_positive(kind) for kind in KINDS for _ in range(2)]
    return {
        "then": "an uploaded artifact is verified and retrieved by its immutable content reference through either backend",
        "passed": all(run["passed"] for run in runs),
        "dimensions": DIMENSIONS,
        "independent_fixtures": runs,
    }


def _rejected(kind: str) -> dict:
    with tempfile.TemporaryDirectory() as root:
        world = World(kind, Path(root))
        committed = world.store.put(world.scope, "kept", OTHER)
        before = _contents(world)
        world.truncate()
        try:
            world.store.put(world.scope, "a1", DATA)
            message = ""
        except base.ArtifactError as error:
            message = str(error)
        run = {
            "backend": kind,
            "refused": message.startswith(f"{kind} acknowledged {len(DATA)} bytes"),
            "protected_state_unchanged": _contents(world) == before,
            "committed_artifact_still_verified": world.store.stat(world.scope, committed) == base.VERIFIED,
            "no_record_for_refused_upload": world.store.recorded(world.scope, "a1") is None,
            "message_discloses_no_content": DATA.decode() not in message,
            "counted": world.store.metrics()[base.METRIC] == {kind: 1},
        }
        corrected = world.store.put(world.scope, "a1", DATA)
        run["corrected_by_new_request"] = world.store.get_range(world.scope, corrected) == DATA
        world.flip = 1
        run["same_length_hash_mismatch_refused"] = _refused(lambda: world.store.put(world.scope, "a3", DATA[::-1]))
        run["hash_mismatch_left_no_record"] = world.store.recorded(world.scope, "a3") is None
        run["passed"] = all(run[name] for name in run if name != "backend")
        run["message"] = message
        run[base.METRIC] = world.store.metrics()[base.METRIC]
        return run


def case_b():
    runs = [_rejected(kind) for kind in KINDS]
    return {
        "then": "a transport acknowledgement with mismatched content hash is not accepted as durability proof",
        "passed": all(run["passed"] for run in runs),
        "dimensions": DIMENSIONS,
        "runs": runs,
    }


def _refused(call, error: type[Exception] = base.ArtifactError) -> bool:
    try:
        call()
    except error:
        return True
    return False


def _recovered(kind: str) -> dict:
    with tempfile.TemporaryDirectory() as root:
        world = World(kind, Path(root))
        real = world.backend.write

        def interrupted(key: str, data: bytes) -> None:
            if data != DATA:
                raise OSError("record upload interrupted")
            real(key, data)

        world.backend.write = interrupted
        interruption = _refused(lambda: world.store.put(world.scope, "a1", DATA), OSError)
        world.backend.write = real
        survived = {
            "object_verified": world.store.stat(world.scope, base.ArtifactRef.of(DATA)) == base.VERIFIED,
            "record_absent": world.store.recorded(world.scope, "a1") is None,
            "staging_absent": not [key for key in world.backend.keys("") if "/staging/" in key],
        }
        restarted = _restarted(world, Path(root))
        ref = restarted.put(world.scope, "a1", DATA)
        before = _contents(world)
        newer = base.Scope.granted(registration(execution="e3", generation=3))
        restarted.commit_manifest(newer, "outputs", ["a1"])
        run = {
            "backend": kind,
            "interrupted_at_record_upload": interruption,
            "after_interruption": survived,
            "restart_completes_the_upload": restarted.recorded(world.scope, "a1") == ref,
            "retry_returns_committed_object": restarted.put(world.scope, "a1", DATA) == ref,
            "retry_added_no_object": {k: v for k, v in _contents(world).items() if "/manifests/" not in k} == before,
            "other_content_under_same_id_refused": _refused(lambda: restarted.put(world.scope, "a1", OTHER)),
            "stale_generation_cannot_replace_newer_manifest": _refused(
                lambda: restarted.commit_manifest(world.scope, "outputs", ["a1"])
            ),
        }
        world.backend.remove(f"s1/t1/objects/{ref.sha256}")
        run["lost_object_reported_absent"] = restarted.stat(world.scope, ref) == base.ABSENT
        run["lost_object_republished_from_matching_content"] = restarted.put(world.scope, "a1", DATA) == ref
        run["passed"] = all(all(v.values()) if isinstance(v, dict) else v for k, v in run.items() if k != "backend")
        return run


def _rollback() -> dict:
    with tempfile.TemporaryDirectory() as root:
        world = World("object-store", Path(root))
        ref = world.store.put(world.scope, "a1", DATA)
        world.store.commit_manifest(world.scope, "outputs", ["a1"])
        published = dict(world.fake.objects)
        writes = world.fake.calls["put_object"] + world.fake.calls["copy_object"] + world.fake.calls["delete_object"]
        previous = base.ArtifactStore(local.LocalBackend(Path(root) / "previous"))
        newer = base.Scope.granted(registration(execution="e3", generation=3))
        previous.put(newer, "a1", world.store.get_range(world.scope, ref))
        manifest = previous.commit_manifest(newer, "outputs", ["a1"])
        key = "s1/t1/manifests/outputs/3.json"
        stored = json.loads(previous.backend.read(key, 0, previous.backend.size(key)))
        after = world.fake.calls["put_object"] + world.fake.calls["copy_object"] + world.fake.calls["delete_object"]
        return {
            "action": "future manifest publication switched from the object store to the previous local adapter",
            "existing_objects_immutable": world.fake.objects == published,
            "no_object_store_writes_after_switch": after == writes,
            "existing_reference_still_verified": world.store.stat(world.scope, ref) == base.VERIFIED,
            "previous_adapter_holds_the_manifest": stored == manifest and manifest["generation"] == 3,
        }


def case_c():
    runs = [_recovered(kind) for kind in KINDS]
    rollback = _rollback()
    return {
        "then": "retrying a completed upload with the same artifact ID returns the committed object without duplication",
        "passed": all(run["passed"] for run in runs) and all(v for k, v in rollback.items() if k != "action"),
        "dimensions": DIMENSIONS,
        "runs": runs,
        "rollback_rehearsal": rollback,
    }
