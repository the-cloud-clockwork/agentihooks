import copy

import pytest

from scripts.swarm_v2.kubernetes.adoption import GROUPS, fields, identity, mismatch
from scripts.swarm_v2.kubernetes.spec import canonical_digest

pytestmark = pytest.mark.unit

HARDENED = {"privileged": False, "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}


def rendered() -> dict:
    return {
        "spec": {
            "serviceAccountName": "swarm-worker",
            "automountServiceAccountToken": False,
            "hostNetwork": False,
            "securityContext": {"runAsNonRoot": True, "runAsUser": 10001},
            "imagePullSecrets": [{"name": "bundle-pull"}],
            "initContainers": [
                {"name": "bundle", "image": "bundle:dev", "command": ["cp", "-R"], "securityContext": HARDENED}
            ],
            "containers": [
                {
                    "name": "agent",
                    "image": "agent@sha256:" + "a" * 64,
                    "args": ["python", "supervisor.py"],
                    "env": [
                        {"name": "TOKEN", "valueFrom": {"secretKeyRef": {"name": "creds", "key": "one"}}},
                        {"name": "BRAIN_URL", "value": "http://brain"},
                    ],
                    "envFrom": [{"secretRef": {"name": "extra"}}, {"configMapRef": {"name": "plain"}}],
                    "volumeMounts": [{"name": "home", "mountPath": "/home/worker"}],
                    "livenessProbe": {"exec": {"command": ["python", "health.py"]}, "periodSeconds": 10},
                    "securityContext": HARDENED,
                }
            ],
            "volumes": [
                {"name": "keys", "secret": {"secretName": "keys"}},
                {"name": "mixed", "projected": {"sources": [{"secret": {"name": "projected"}}, {"configMap": {}}]}},
                {"name": "home", "emptyDir": {"sizeLimit": "1024Mi"}},
            ],
        }
    }


def live() -> dict:
    pod = copy.deepcopy(rendered())
    spec = pod["spec"]
    del spec["hostNetwork"]
    spec.update({"dnsPolicy": "ClusterFirst", "nodeName": "node-1", "overhead": {"cpu": "250m"}})
    spec["volumes"][2]["emptyDir"]["sizeLimit"] = "1Gi"
    for container in spec["containers"] + spec["initContainers"]:
        container["terminationMessagePath"] = "/dev/termination-log"
        container["resources"] = {"limits": {"ephemeral-storage": "2Gi"}}
    spec["containers"][0]["livenessProbe"].update(successThreshold=1, timeoutSeconds=1)
    return pod


def test_a_live_pod_with_only_admission_defaults_matches():
    assert mismatch(rendered(), live()) == ""


def test_pull_secrets_the_render_never_named_are_refused():
    expected, pod = rendered(), live()
    del expected["spec"]["imagePullSecrets"]
    pod["spec"]["imagePullSecrets"] = [{"name": "account-pull"}]
    assert mismatch(expected, pod) == "secrets"


def test_identity_ignores_admission_defaults_and_names_the_pod_digest():
    assert identity("sha256:pod", rendered()["spec"]) == identity("sha256:pod", live()["spec"])
    assert identity("sha256:pod", rendered()["spec"]) != identity("sha256:other", live()["spec"])
    changed = live()
    changed["spec"]["containers"][0]["image"] = "agent:evil"
    assert identity("sha256:pod", rendered()["spec"]) != identity("sha256:pod", changed["spec"])


def test_fields_names_every_group_in_comparison_order():
    assert (
        tuple(fields({}))
        == GROUPS
        == (
            "image",
            "arguments",
            "secrets",
            "environment",
            "service-account",
            "volumes",
            "network",
            "security-context",
        )
    )


@pytest.mark.parametrize(
    ("quantity", "size"),
    [("1Gi", 1024**3), ("1024Mi", 1024**3), ("2k", 2000), ("3M", 3 * 1000**2), ("512", 512), ("1.5Gi", "1.5Gi")],
)
def test_emptydir_sizes_compare_as_bytes(quantity, size):
    spec = {"volumes": [{"name": "v", "emptyDir": {"sizeLimit": quantity}}]}
    assert fields(spec)["volumes"][0] == [{"name": "v", "emptyDir": {"sizeLimit": size}}]


def test_a_volume_without_a_size_is_compared_as_written():
    spec = {"volumes": [{"name": "v", "emptyDir": {"medium": "Memory"}}, {"name": "c", "configMap": {"name": "x"}}]}
    assert fields(spec)["volumes"][0] == spec["volumes"]


def change(path, value):
    def apply(spec):
        *parents, last = path
        for key in parents:
            spec = spec[key]
        if value is None:
            del spec[last]
        else:
            spec[last] = value

    return apply


@pytest.mark.parametrize(
    ("tamper", "reason"),
    [
        (change(("initContainers", 0, "image"), "bundle:evil"), "image"),
        (lambda spec: spec["containers"].append({"name": "sidecar", "image": "proxy"}), "image"),
        (change(("initContainers", 0, "command"), ["sh", "-c", "id"]), "arguments"),
        (change(("containers", 0, "args"), None), "arguments"),
        (change(("containers", 0, "env", 0, "valueFrom", "secretKeyRef", "key"), "two"), "secrets"),
        (change(("containers", 0, "envFrom", 0, "secretRef", "name"), "other"), "secrets"),
        (change(("volumes", 0, "secret", "secretName"), "other"), "secrets"),
        (change(("volumes", 1, "projected", "sources", 0, "secret", "name"), "other"), "secrets"),
        (change(("imagePullSecrets",), [{"name": "other"}]), "secrets"),
        (change(("imagePullSecrets",), None), "secrets"),
        (change(("serviceAccountName",), "controller"), "service-account"),
        (change(("automountServiceAccountToken",), True), "service-account"),
        (change(("securityContext", "runAsUser"), 0), "security-context"),
        (change(("securityContext",), None), "security-context"),
        (change(("hostNetwork",), True), "security-context"),
        (change(("hostPID",), True), "security-context"),
        (change(("hostIPC",), True), "security-context"),
        (
            change(("containers", 0, "securityContext", "capabilities"), {"drop": ["ALL"], "add": ["SYS_ADMIN"]}),
            "security-context",
        ),
        (change(("initContainers", 0, "securityContext", "privileged"), True), "security-context"),
        (change(("containers", 0, "securityContext"), None), "security-context"),
        (change(("containers", 0, "securityContext"), {**HARDENED, "runAsUser": 0}), "security-context"),
        (
            change(("containers", 0, "securityContext"), {**HARDENED, "seccompProfile": {"type": "Unconfined"}}),
            "security-context",
        ),
        (change(("securityContext", "seLinuxOptions"), {"type": "spc_t"}), "security-context"),
        (
            change(("securityContext", "sysctls"), [{"name": "kernel.shm_rmid_forced", "value": "0"}]),
            "security-context",
        ),
        (change(("securityContext", "supplementalGroups"), [0]), "security-context"),
        (change(("shareProcessNamespace",), True), "security-context"),
        (change(("runtimeClassName",), "runc"), "security-context"),
        (lambda spec: spec["volumes"].append({"name": "root", "hostPath": {"path": "/"}}), "volumes"),
        (change(("volumes", 2, "emptyDir", "medium"), "Memory"), "volumes"),
        (lambda spec: spec["containers"][0]["volumeMounts"].append({"name": "root", "mountPath": "/host"}), "volumes"),
        (lambda spec: spec.update(ephemeralContainers=[{"name": "debug", "image": "busybox"}]), "image"),
        (change(("containers", 0, "env", 1, "value"), "http://attacker"), "environment"),
        (change(("containers", 0, "livenessProbe", "exec", "command"), ["sh", "-c", "curl evil"]), "arguments"),
        (change(("containers", 0, "livenessProbe", "httpGet"), {"port": 80}), "arguments"),
        (change(("containers", 0, "lifecycle"), {"postStart": {"exec": {"command": ["id"]}}}), "arguments"),
        (change(("containers", 0, "workingDir"), "/tmp"), "arguments"),
        (change(("volumes", 2, "emptyDir", "sizeLimit"), "1Ti"), "volumes"),
        (change(("volumes", 2, "emptyDir", "sizeLimit"), "1.5Gi"), "volumes"),
        (change(("hostAliases",), [{"ip": "10.0.0.9", "hostnames": ["brain"]}]), "network"),
        (change(("dnsConfig",), {"nameservers": ["10.0.0.9"]}), "network"),
        (change(("containers", 0, "ports"), [{"containerPort": 80, "hostPort": 80}]), "network"),
        (change(("containers", 0, "ports"), [{"containerPort": 80, "hostIP": "0.0.0.0"}]), "network"),
        (change(("hostUsers",), True), "security-context"),
        (change(("containers", 0, "envFrom", 1, "configMapRef", "name"), "other"), "environment"),
    ],
)
def test_each_live_field_difference_is_named(tamper, reason):
    pod = live()
    tamper(pod["spec"])
    assert mismatch(rendered(), pod) == reason


def test_a_pod_without_a_spec_differs_by_image():
    assert mismatch(rendered(), {}) == "image"


def test_a_render_without_a_spec_matches_a_pod_without_one():
    assert mismatch({}, {}) == ""


NO_HANDLERS = {"exec": None, "httpGet": None, "tcpSocket": None, "grpc": None}
SNAPSHOT_SPEC = {
    "imagePullSecrets": [{"name": "pull"}],
    "initContainers": [
        {
            "name": "c",
            "env": [{"name": "B", "valueFrom": {"secretKeyRef": {"name": "t", "key": "j"}}}],
            "envFrom": [{"secretRef": {"name": "eg"}}],
            "ports": [{"hostPort": 2}],
        }
    ],
    "containers": [
        {
            "image": "i",
            "env": [
                {"name": "A", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}},
                {"valueFrom": {"secretKeyRef": {"name": "s", "key": "k2"}}},
            ],
            "envFrom": [{"secretRef": {"name": "ef"}}],
            "ports": [{"hostPort": 1, "hostIP": "h"}],
            "securityContext": {"a": 1},
        }
    ],
    "volumes": [
        {"name": "v", "secret": {"secretName": "vs"}},
        {"name": "p", "projected": {"sources": [{"secret": {"name": "ps"}}]}},
    ],
    "securityContext": {"b": 2},
    "hostUsers": False,
    "runtimeClassName": "r",
    "serviceAccountName": "sa",
    "automountServiceAccountToken": False,
}


def test_fields_project_named_and_unnamed_containers_exactly():
    init, container = SNAPSHOT_SPEC["initContainers"][0], SNAPSHOT_SPEC["containers"][0]
    probes = {probe: NO_HANDLERS for probe in ("startupProbe", "readinessProbe", "livenessProbe")}
    assert fields(SNAPSHOT_SPEC) == {
        "image": {"": ["i"], "c": [None]},
        "arguments": {"c": [None, None, None, None, probes], "": [None, None, None, None, probes]},
        "secrets": [
            ["env", "", "A", "s", "k"],
            ["env", "", None, "s", "k2"],
            ["env", "c", "B", "t", "j"],
            ["envFrom", "", "ef"],
            ["envFrom", "c", "eg"],
            ["projected", "p", "ps"],
            ["pull", "pull"],
            ["volume", "v", "vs"],
        ],
        "environment": {"": [container["env"], container["envFrom"]], "c": [init["env"], init["envFrom"]]},
        "service-account": ["sa", False],
        "volumes": [SNAPSHOT_SPEC["volumes"], {"": [None], "c": [None]}],
        "network": [None, None, {"": [[1, "h"]], "c": [[2, None]]}],
        "security-context": {
            "pod": {"b": 2},
            "containers": {"": [{"a": 1}], "c": [None]},
            "host": [False, False, False, False],
            "hostUsers": False,
            "runtimeClassName": "r",
        },
    }


def test_identity_digests_the_pod_digest_and_the_fields():
    assert identity("p", SNAPSHOT_SPEC) == canonical_digest({"pod": "p", "fields": fields(SNAPSHOT_SPEC)})
