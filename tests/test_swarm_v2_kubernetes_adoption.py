import copy

import pytest

from scripts.swarm_v2.kubernetes.adoption import mismatch

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
    spec["dnsPolicy"] = "ClusterFirst"
    spec["securityContext"]["seLinuxOptions"] = {"level": "s0"}
    for container in spec["containers"] + spec["initContainers"]:
        container["terminationMessagePath"] = "/dev/termination-log"
        container["securityContext"] = {**container["securityContext"], "procMount": "Default"}
    return pod


def test_a_live_pod_with_only_admission_defaults_matches():
    assert mismatch(rendered(), live()) == ""


def test_service_account_pull_secrets_are_tolerated_when_the_render_names_none():
    expected, pod = rendered(), live()
    del expected["spec"]["imagePullSecrets"]
    pod["spec"]["imagePullSecrets"] = [{"name": "account-pull"}]
    assert mismatch(expected, pod) == ""


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
    ],
)
def test_each_live_field_difference_is_named(tamper, reason):
    pod = live()
    tamper(pod["spec"])
    assert mismatch(rendered(), pod) == reason


def test_a_pod_without_a_spec_differs_by_image():
    assert mismatch(rendered(), {}) == "image"
