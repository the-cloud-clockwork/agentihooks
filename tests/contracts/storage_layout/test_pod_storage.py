import copy
import json
from pathlib import Path

import pytest
from scripts.swarm_v2.kubernetes.storage import MountChecker, StorageRefused

from scripts.swarm_v2.kubernetes import storage
from scripts.swarm_v2.kubernetes.spec import PodSpecRefused, PodTemplate, load_policy

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parents[2] / "fixtures" / "swarm_v2"
EXECUTION = "exe-0f1e2d3c4b5a69788796a5b4c3d2e1f0"
SEED = {"name": "profiles", "purpose": "seed", "mount_path": "/opt/swarm-seed", "source": {"claim": "swarm-seed"}}
ARTIFACTS = {
    "name": "artifacts",
    "purpose": "artifact",
    "mount_path": "/home/worker/artifacts",
    "source": {"nfs": {"server": "nas.anton.lan", "path": "/export/swarm-artifacts"}},
}
CACHE = {
    "name": "node-cache",
    "purpose": "cache",
    "mount_path": "/home/worker/cache",
    "source": {"host_path": "/var/lib/swarm-cache"},
}
OPERATOR_HOME = {
    "name": "operator",
    "purpose": "seed",
    "mount_path": "/home/worker/operator",
    "source": {"host_path": "/home/iamroot"},
}


def policy(*mounts: dict) -> dict:
    document = json.loads((FIXTURES / "pod-policy.json").read_text())
    if mounts:
        document["mounts"] = [copy.deepcopy(mount) for mount in mounts]
    return document


def launch() -> dict:
    return json.loads((FIXTURES / "pod-launch.json").read_text())


def load(tmp_path, document: dict) -> dict:
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(document))
    return load_policy(path)


def safe_pod(tmp_path) -> dict:
    return PodTemplate(load(tmp_path, policy(SEED, ARTIFACTS, CACHE))).render(launch()).pod


def agent(pod: dict) -> dict:
    return pod["spec"]["containers"][0]


def volume(pod: dict, name: str) -> dict:
    return next(found for found in pod["spec"]["volumes"] if found["name"] == name)


def mount(pod: dict, name: str) -> dict:
    return next(found for found in agent(pod)["volumeMounts"] if found["name"] == name)


def add(pod: dict, shared: dict, at: dict) -> dict:
    changed = copy.deepcopy(pod)
    changed["spec"]["volumes"].append(shared)
    agent(changed)["volumeMounts"].append(at)
    return changed


def refused(pod: dict, checker: MountChecker | None = None) -> StorageRefused:
    with pytest.raises(StorageRefused) as error:
        (checker or MountChecker()).check(pod)
    return error.value


def test_a_policy_without_shared_mounts_renders_only_the_private_volumes(tmp_path):
    pod = PodTemplate(load(tmp_path, policy())).render(launch()).pod
    assert [found["name"] for found in pod["spec"]["volumes"]] == ["home", "tmp", "launch", "credential"]
    MountChecker().check(pod)


def test_a_seed_renders_read_only_on_the_volume_and_the_mount(tmp_path):
    pod = safe_pod(tmp_path)
    assert volume(pod, "shared-profiles") == {
        "name": "shared-profiles",
        "persistentVolumeClaim": {"claimName": "swarm-seed", "readOnly": True},
    }
    assert mount(pod, "shared-profiles") == {
        "name": "shared-profiles",
        "mountPath": "/opt/swarm-seed",
        "readOnly": True,
    }


def test_an_artifact_mount_is_writable_only_inside_its_execution_subdirectory(tmp_path):
    pod = safe_pod(tmp_path)
    assert volume(pod, "shared-artifacts") == {
        "name": "shared-artifacts",
        "nfs": {"server": "nas.anton.lan", "path": "/export/swarm-artifacts", "readOnly": False},
    }
    assert mount(pod, "shared-artifacts") == {
        "name": "shared-artifacts",
        "mountPath": "/home/worker/artifacts",
        "readOnly": False,
        "subPath": EXECUTION,
    }


def test_the_node_cache_store_is_mounted_read_only_into_every_attempt(tmp_path):
    pod = safe_pod(tmp_path)
    assert volume(pod, "shared-node-cache") == {
        "name": "shared-node-cache",
        "hostPath": {"path": "/var/lib/swarm-cache", "type": "Directory"},
    }
    assert mount(pod, "shared-node-cache") == {
        "name": "shared-node-cache",
        "mountPath": "/home/worker/cache",
        "readOnly": True,
    }


def test_a_safe_deployment_with_shared_storage_and_private_homes_passes(tmp_path):
    checker = MountChecker()
    checker.check(safe_pod(tmp_path))
    assert checker.shared_runtime_mount_rejections_total() == {}


def test_a_writable_node_cache_mount_is_refused_so_an_attempt_cannot_write_a_seed(tmp_path):
    pod = safe_pod(tmp_path)
    mount(pod, "shared-node-cache")["readOnly"] = False
    error = refused(pod)
    assert error.reason == "unscoped_shared_write"
    assert str(error) == (
        "container agent mounts shared volume shared-node-cache writable at /home/worker/cache "
        "without a subPath of its execution"
    )


@pytest.mark.parametrize(
    "path",
    [
        "/home/worker/.codex",
        "/home/worker/.claude",
        "/srv/state/.config/herdr",
        "/srv/.local/share",
        "/opt/herdr",
        "/home/worker",
        "/home",
        "/",
        f"/home/worker/attempts/{EXECUTION}/homes",
        "/home/worker/attempts",
        "/tmp",
        "/var/run/swarm/launch",
        "/var/run",
    ],
)
def test_a_shared_writable_mount_over_a_native_runtime_root_is_refused(tmp_path, path):
    checker = MountChecker()
    pod = add(
        safe_pod(tmp_path),
        {"name": "codex-home", "persistentVolumeClaim": {"claimName": "codex-home"}},
        {"name": "codex-home", "mountPath": path, "subPath": EXECUTION},
    )
    error = refused(pod, checker)
    assert error.reason == "shared_runtime"
    assert str(error) == (
        f"container agent mounts shared volume codex-home writable at {path}, "
        "over a native runtime root that stays private to the attempt"
    )
    assert checker.shared_runtime_mount_rejections_total() == {"shared_runtime": 1}


@pytest.mark.parametrize("path", ["/home/worker/codex", "/home/worker/artifacts/.codexrc", "/srv/herdr-logs"])
def test_paths_beside_the_runtime_roots_are_not_runtime_roots(tmp_path, path):
    pod = add(
        safe_pod(tmp_path),
        {"name": "out", "persistentVolumeClaim": {"claimName": "out"}},
        {"name": "out", "mountPath": path, "subPath": EXECUTION},
    )
    MountChecker().check(pod)


@pytest.mark.parametrize(
    "shared",
    [
        {"persistentVolumeClaim": {"claimName": "x"}},
        {"nfs": {"server": "nas", "path": "/x"}},
        {"hostPath": {"path": "/var/lib/x"}},
        {"cephfs": {"monitors": ["m"]}},
        {"csi": {"driver": "x"}},
        {},
    ],
)
def test_every_volume_kind_outside_the_private_ones_counts_as_shared(tmp_path, shared):
    pod = add(safe_pod(tmp_path), {"name": "x", **shared}, {"name": "x", "mountPath": "/srv/x"})
    assert refused(pod).reason == "unscoped_shared_write"


@pytest.mark.parametrize(
    "private",
    [
        {"emptyDir": {}},
        {"configMap": {"name": "x"}},
        {"secret": {"secretName": "x"}},
        {"projected": {"sources": []}},
        {"downwardAPI": {"items": []}},
        {"ephemeral": {"volumeClaimTemplate": {}}},
    ],
)
def test_private_volume_kinds_may_be_mounted_writable_anywhere(tmp_path, private):
    pod = add(safe_pod(tmp_path), {"name": "x", **private}, {"name": "x", "mountPath": "/home/worker/.codex"})
    MountChecker().check(pod)


def test_a_mount_naming_a_missing_volume_counts_as_shared(tmp_path):
    pod = copy.deepcopy(safe_pod(tmp_path))
    agent(pod)["volumeMounts"].append({"name": "ghost", "mountPath": "/srv/ghost"})
    assert refused(pod).reason == "unscoped_shared_write"


@pytest.mark.parametrize(
    "source",
    [
        {"persistentVolumeClaim": {"claimName": "x", "readOnly": True}},
        {"nfs": {"server": "nas", "path": "/x", "readOnly": True}},
    ],
)
def test_a_volume_marked_read_only_at_its_source_is_a_seed(tmp_path, source):
    pod = add(safe_pod(tmp_path), {"name": "x", **source}, {"name": "x", "mountPath": "/home/worker/.codex"})
    MountChecker().check(pod)


def test_a_read_only_mount_over_a_runtime_root_is_a_seed(tmp_path):
    pod = add(
        safe_pod(tmp_path),
        {"name": "x", "persistentVolumeClaim": {"claimName": "x"}},
        {"name": "x", "mountPath": "/home/worker/.claude", "readOnly": True},
    )
    MountChecker().check(pod)


@pytest.mark.parametrize(
    "sub_path",
    [None, "", "shared", "exe-0f1e2d3c4b5a69788796a5b4c3d2e1f", EXECUTION + "x", "x/" + EXECUTION, EXECUTION + "/../x"],
)
def test_a_shared_write_needs_a_subpath_of_its_own_execution(tmp_path, sub_path):
    at = {"name": "x", "mountPath": "/srv/x"} | ({} if sub_path is None else {"subPath": sub_path})
    pod = add(safe_pod(tmp_path), {"name": "x", "persistentVolumeClaim": {"claimName": "x"}}, at)
    assert refused(pod).reason == "unscoped_shared_write"


def test_a_subpath_expression_is_not_a_proven_scope(tmp_path):
    at = {"name": "x", "mountPath": "/srv/x", "subPathExpr": "$(EXECUTION_ID)"}
    pod = add(safe_pod(tmp_path), {"name": "x", "persistentVolumeClaim": {"claimName": "x"}}, at)
    assert refused(pod).reason == "unscoped_shared_write"


def test_a_subdirectory_of_its_own_execution_is_a_scoped_write(tmp_path):
    at = {"name": "x", "mountPath": "/srv/x", "subPath": EXECUTION + "/checkpoints"}
    MountChecker().check(add(safe_pod(tmp_path), {"name": "x", "persistentVolumeClaim": {"claimName": "x"}}, at))


def test_a_pod_without_an_execution_label_has_no_scoped_write(tmp_path):
    pod = copy.deepcopy(safe_pod(tmp_path))
    del pod["metadata"]["labels"][storage.EXECUTION_LABEL]
    assert refused(pod).reason == "unscoped_shared_write"


def test_init_containers_are_checked_like_the_agent(tmp_path):
    pod = copy.deepcopy(safe_pod(tmp_path))
    pod["spec"]["volumes"].append({"name": "x", "persistentVolumeClaim": {"claimName": "x"}})
    pod["spec"]["initContainers"] = [{"name": "prepare", "volumeMounts": [{"name": "x", "mountPath": "/home/worker"}]}]
    error = refused(pod)
    assert (error.reason, str(error).split(" mounts ")[0]) == ("shared_runtime", "container prepare")


@pytest.mark.parametrize(
    "path", ["/home/iamroot", "/home/iamroot/", "/home", "/root", "/Users/op", "/", "/home/./iamroot"]
)
def test_the_operator_home_from_the_node_is_refused_even_read_only(tmp_path, path):
    checker = MountChecker()
    pod = add(
        safe_pod(tmp_path),
        {"name": "op", "hostPath": {"path": path}},
        {"name": "op", "mountPath": "/home/worker/operator", "readOnly": True},
    )
    error = refused(pod, checker)
    assert error.reason == "operator_home"
    assert str(error) == (
        "volume op mounts the operator home from the node; give workers private homes and a read only seed instead"
    )
    assert checker.shared_runtime_mount_rejections_total() == {"operator_home": 1}


def test_an_unmounted_operator_home_volume_is_still_refused(tmp_path):
    pod = copy.deepcopy(safe_pod(tmp_path))
    pod["spec"]["volumes"].append({"name": "op", "hostPath": {"path": "/home/iamroot"}})
    assert refused(pod).reason == "operator_home"


@pytest.mark.parametrize("path", ["/home/iamroot/dev/seed", "/var/lib/swarm-cache", "/Users/op/x"])
def test_a_folder_inside_an_operator_home_is_not_the_whole_home(tmp_path, path):
    pod = add(
        safe_pod(tmp_path),
        {"name": "seed", "hostPath": {"path": path}},
        {"name": "seed", "mountPath": "/opt/seed", "readOnly": True},
    )
    MountChecker().check(pod)


def test_rejections_count_by_reason_across_checks(tmp_path):
    checker = MountChecker()
    unscoped = add(
        safe_pod(tmp_path), {"name": "x", "nfs": {"server": "n", "path": "/x"}}, {"name": "x", "mountPath": "/x"}
    )
    for _ in range(2):
        refused(unscoped, checker)
    checker.check(safe_pod(tmp_path))
    assert checker.shared_runtime_mount_rejections_total() == {"unscoped_shared_write": 2}


def test_mounting_the_operator_home_into_all_workers_is_refused_by_the_template(tmp_path):
    template = PodTemplate(load(tmp_path, policy(SEED, OPERATOR_HOME)))
    with pytest.raises(PodSpecRefused) as error:
        template.render(launch())
    assert error.value.reason == "storage"
    assert str(error.value) == (
        "volume shared-operator mounts the operator home from the node; "
        "give workers private homes and a read only seed instead"
    )
    assert template.pod_spec_validation_failures_total() == {"storage": 1}
    assert template.shared_runtime_mount_rejections_total() == {"operator_home": 1}


def test_an_artifact_mount_over_a_native_home_is_refused_by_the_template(tmp_path):
    codex = {**ARTIFACTS, "name": "codex", "mount_path": "/home/worker/.codex"}
    template = PodTemplate(load(tmp_path, policy(codex)))
    with pytest.raises(PodSpecRefused) as error:
        template.render(launch())
    assert error.value.reason == "storage"
    assert template.shared_runtime_mount_rejections_total() == {"shared_runtime": 1}


@pytest.mark.parametrize(
    "change",
    [
        {"purpose": "home"},
        {"mount_path": "relative"},
        {"name": "Bad_Name"},
        {"source": {}},
        {"source": {"claim": "a", "host_path": "/b"}},
        {"source": {"host_path": "relative"}},
        {"source": {"nfs": {"server": "nas"}}},
        {"extra": True},
    ],
)
def test_the_policy_refuses_a_malformed_mount(tmp_path, change):
    with pytest.raises(PodSpecRefused) as error:
        load(tmp_path, policy({**SEED, **change}))
    assert error.value.reason == "policy"


def test_the_checker_command_passes_a_safe_pod_and_refuses_an_unsafe_one(tmp_path, capsys):
    safe = tmp_path / "safe.json"
    safe.write_text(json.dumps(safe_pod(tmp_path)))
    assert storage.main(["check", str(safe)]) == 0
    unsafe = tmp_path / "unsafe.json"
    codex = {"name": "codex", "mountPath": "/home/worker/.codex"}
    unsafe.write_text(
        json.dumps(add(safe_pod(tmp_path), {"name": "codex", "nfs": {"server": "n", "path": "/c"}}, codex))
    )
    assert storage.main(["check", str(unsafe)]) == 1
    assert capsys.readouterr().err == (
        "container agent mounts shared volume codex writable at /home/worker/.codex, "
        "over a native runtime root that stays private to the attempt\n"
    )


def test_the_checker_command_refuses_an_unreadable_file(tmp_path, capsys):
    assert storage.main(["check", str(tmp_path / "absent.json")]) == 1
    assert capsys.readouterr().err == f"{tmp_path / 'absent.json'} is not a readable JSON file\n"


def test_the_checker_command_reports_usage_errors():
    assert storage.main([]) == 64
    assert storage.main(["--help"]) == 0
