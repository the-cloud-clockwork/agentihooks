import copy
import tempfile
from pathlib import Path

from scripts.swarm_v2 import cache
from scripts.swarm_v2.artifacts import base, publication
from scripts.swarm_v2.artifacts.local import LocalBackend
from scripts.swarm_v2.kubernetes.spec import PodSpecRefused, PodTemplate
from scripts.swarm_v2.kubernetes.storage import MountChecker, StorageRefused
from tests.contracts.storage_layout.test_pod_storage import (
    ARTIFACTS,
    CACHE,
    OPERATOR_HOME,
    SEED,
    add,
    launch,
    load,
    policy,
    safe_pod,
)
from tests.contracts.storage_layout.test_publication import DATA, SCOPE, lose, restore
from tests.test_swarm_v2_cache import build, fill, key, published, read_only, stamped

DIMENSIONS = {"fixture": "sv2-fsy-05"}
METRIC = "shared_runtime_mount_rejections_total"
CODEX_HOME = {"name": "codex-home", "persistentVolumeClaim": {"claimName": "codex-home"}}


def _deployment() -> dict:
    with tempfile.TemporaryDirectory() as root:
        checker = MountChecker()
        safe = safe_pod(Path(root))
        checker.check(safe)
        unsafe = add(safe, CODEX_HOME, {"name": "codex-home", "mountPath": "/home/worker/.codex"})
        try:
            checker.check(unsafe)
            reason = None
        except StorageRefused as refused:
            reason = refused.reason
        mounts = {m["name"]: m for m in safe["spec"]["containers"][0]["volumeMounts"]}
        run = {
            "safe_deployment_passes": True,
            "seed_read_only": mounts["shared-profiles"]["readOnly"] is True,
            "artifact_scoped_to_execution": mounts["shared-artifacts"].get("subPath") == launch()["execution_id"],
            "node_cache_read_only": mounts["shared-node-cache"]["readOnly"] is True,
            "runtime_homes_private": [v["name"] for v in safe["spec"]["volumes"][:2]] == ["home", "tmp"],
            "shared_writable_codex_home_refused": reason == "shared_runtime",
        }
        run["passed"] = all(run.values())
        run[METRIC] = checker.shared_runtime_mount_rejections_total()
        return run


def case_a() -> dict:
    runs = [_deployment() for _ in range(2)]
    return {
        "then": "a deployment with shared artifact storage and private runtime homes passes while a shared writable Codex home fails policy validation",
        "passed": all(run["passed"] and run[METRIC] == {"shared_runtime": 1} for run in runs),
        "dimensions": DIMENSIONS,
        "independent_fixtures": runs,
    }


def _operator_home() -> dict:
    with tempfile.TemporaryDirectory() as root:
        document = policy(SEED, ARTIFACTS, CACHE, OPERATOR_HOME)
        loaded = load(Path(root), document)
        before = copy.deepcopy(loaded)
        template = PodTemplate(loaded)
        try:
            template.render(launch())
            message, reason = "", None
        except PodSpecRefused as refused:
            message, reason = str(refused), refused.reason
        corrected = PodTemplate(load(Path(root), policy(SEED, ARTIFACTS, CACHE))).render(launch())
        return {
            "operator_home_refused": reason == "storage",
            "policy_unchanged": loaded == before,
            "no_host_path_disclosed": "/home/iamroot" not in message,
            "correction_needs_a_new_policy": corrected.pod["metadata"]["name"].startswith("swarm-"),
            "validation_failures": template.pod_spec_validation_failures_total(),
            METRIC: template.shared_runtime_mount_rejections_total(),
        }


def _seed_write() -> dict:
    with tempfile.TemporaryDirectory() as root:
        world = build(Path(root))
        published(world, key(), {"a.whl": b"owner"})
        before = stamped(world.store.policy.store)
        layer = cache.attach(read_only(world), world.second, key(lock="b" * 64))
        fill(layer, {"a.whl": b"attempt"})
        try:
            cache.publish(read_only(world), world.second, layer)
            refused = False
        except cache.CacheError:
            refused = True
        return {"attempt_seed_write_refused": refused, "store_unchanged": stamped(world.store.policy.store) == before}


def case_b() -> dict:
    home, seed = _operator_home(), _seed_write()
    checks = [value for value in (*home.values(), *seed.values()) if isinstance(value, bool)]
    return {
        "then": "mounting the entire operator home into all workers is refused by the template checker",
        "passed": all(checks)
        and home["validation_failures"] == {"storage": 1}
        and home[METRIC] == {"operator_home": 1},
        "dimensions": DIMENSIONS,
        "operator_home": home,
        "node_cache_seed": seed,
    }


def case_c() -> dict:
    with tempfile.TemporaryDirectory() as root:
        worktree, shared = Path(root) / "worktree", Path(root) / "shared"
        worktree.mkdir()
        shared.mkdir()
        (worktree / "diff.patch").write_bytes(DATA)
        store = base.ArtifactStore(LocalBackend(shared))
        before = stamped(worktree)
        lost = lose(shared)
        paused = publication.publish(store, SCOPE, "ckpt-1", worktree / "diff.patch")
        worktree_intact = stamped(worktree) == before
        restore(shared, lost)
        first = publication.publish(store, SCOPE, "ckpt-1", worktree / "diff.patch")
        keys = store.backend.keys("")
        replay = publication.publish(store, SCOPE, "ckpt-1", worktree / "diff.patch")
        run = {
            "publication_paused_on_loss": paused.state == publication.PAUSED and paused.ref is None,
            "worktree_intact_after_loss": worktree_intact,
            "published_after_restore": first.state == publication.PUBLISHED,
            "replay_is_the_same_reference": replay == first,
            "no_duplicate_objects": store.backend.keys("") == keys and len(keys) == 2,
            "worktree_intact_after_replay": stamped(worktree) == before,
        }
        return {
            "then": "loss of shared artifact storage degrades checkpoint publication without corrupting local worktrees",
            "passed": all(run.values()),
            "dimensions": DIMENSIONS,
            "paused_reason": paused.reason,
            "run": run,
            METRIC: {},
        }
