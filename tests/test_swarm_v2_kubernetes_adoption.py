import copy

import pytest

from scripts.swarm_v2.kubernetes.adoption import GROUPS, fields, identity, mismatch

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
                    "securityContext": HARDENED,
                }
            ],
            "volumes": [
                {"name": "keys", "secret": {"secretName": "keys"}},
                {"name": "mixed", "projected": {"sources": [{"secret": {"name": "projected"}}, {"configMap": {}}]}},
                {"name": "home", "emptyDir": {}},
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
            "security-context",
        )
    )


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
        (change(("containers", 0, "envFrom", 1, "configMapRef", "name"), "other"), "environment"),
    ],
)
def test_each_live_field_difference_is_named(tamper, reason):
    pod = live()
    tamper(pod["spec"])
    assert mismatch(rendered(), pod) == reason


def test_a_pod_without_a_spec_differs_by_image():
    assert mismatch(rendered(), {}) == "image"
