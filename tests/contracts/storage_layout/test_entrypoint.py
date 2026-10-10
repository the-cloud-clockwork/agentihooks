import pytest

from scripts.swarm_v2.artifacts import base, benchmark, publication
from scripts.swarm_v2.artifacts.local import LocalBackend
from scripts.swarm_v2.kubernetes.runtime import KubernetesTransport
from scripts.swarm_v2.kubernetes.spec import PodTemplate
from scripts.swarm_v2.runtime.operations import Observation, Operation, Phase
from tests.contracts.storage_layout.test_pod_storage import (
    ARTIFACTS,
    CACHE,
    EXECUTION,
    OPERATOR_HOME,
    SEED,
    launch,
    load,
    policy,
)
from tests.contracts.storage_layout.test_publication import DATA
from tests.sv2_kub02_cases import ApiServer
from tests.test_swarm_v2_artifacts import Authority, bound
from tests.test_swarm_v2_cache import stamped

pytestmark = pytest.mark.unit


def spawn() -> Operation:
    return Operation("op-1", EXECUTION, 3, "spawn", "kubernetes", "payload-digest", {})


def sender(tmp_path, *mounts) -> KubernetesTransport:
    document = policy(*mounts)
    return KubernetesTransport(ApiServer(document["namespace"]), "fixture", PodTemplate(load(tmp_path, document)))


def test_the_pod_create_path_refuses_an_operator_home_mount_without_calling_the_api(tmp_path):
    unsafe = sender(tmp_path, SEED, ARTIFACTS, CACHE, OPERATOR_HOME)
    assert unsafe.apply_operation(spawn(), launch()) == Observation(Phase.REFUSED)
    assert unsafe.api.create_calls == 0
    assert unsafe.template.pod_spec_validation_failures_total() == {"storage": 1}
    assert unsafe.template.shared_runtime_mount_rejections_total() == {"operator_home": 1}


def test_the_pod_create_path_creates_a_pod_with_a_read_only_seed_and_a_scoped_artifact_mount(tmp_path):
    safe = sender(tmp_path, SEED, ARTIFACTS, CACHE)
    assert safe.apply_operation(spawn(), launch()).phase == Phase.APPLIED
    (pod,) = safe.api.objects.values()
    mounts = {m["name"]: m for m in pod["spec"]["containers"][0]["volumeMounts"]}
    assert mounts["shared-profiles"]["readOnly"] is True
    assert mounts["shared-node-cache"]["readOnly"] is True
    assert mounts["shared-artifacts"]["subPath"] == EXECUTION
    assert safe.template.shared_runtime_mount_rejections_total() == {}


def test_publication_takes_its_scope_from_the_grant_and_refuses_a_display_label(tmp_path):
    (tmp_path / "attempt.bin").write_bytes(DATA)
    shared = tmp_path / "shared"
    store = bound(LocalBackend(shared), task="t9", execution="e9")
    assert publication.publish(store, "ckpt-1", tmp_path / "attempt.bin", {}).state == publication.PUBLISHED
    assert store.scope == base.Scope("s1", "t9", "e9", 2)
    assert sorted(p.name for p in (shared / "s1").iterdir()) == ["t9"]
    with pytest.raises(base.ArtifactError) as error:
        base.ArtifactStore(LocalBackend(shared), Authority({"seat": "eng-1", "task": "t9"}), "eng-1")
    assert str(error.value) == base.UNGRANTED


def test_rollback_keeps_every_committed_artifact_in_the_store(tmp_path):
    (tmp_path / "attempt.bin").write_bytes(DATA)
    store = bound(LocalBackend(tmp_path / "shared"))
    committed = publication.publish(store, "ckpt-1", tmp_path / "attempt.bin", {}).ref
    before = stamped(tmp_path / "shared")
    (tmp_path / "attempt.bin").write_bytes(DATA + b"later")
    paused = publication.publish(store, "ckpt-2", tmp_path / "attempt.bin", {publication.SWITCH: "off"})
    assert paused.state == publication.PAUSED
    assert stamped(tmp_path / "shared") == before
    assert store.stat(committed) == base.VERIFIED


def test_the_benchmark_removes_only_its_own_subfolder(tmp_path):
    mount = tmp_path / "mount"
    (mount / "s1").mkdir(parents=True)
    (mount / "s1" / "record.json").write_bytes(DATA)
    before = stamped(mount)
    benchmark.run(mount, [1], 1)
    assert stamped(mount) == before
