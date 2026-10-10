"""Render an admitted launch into one bounded execution Pod; the task payload reaches the Pod only as a digest."""

import argparse
import copy
import hashlib
import json
import re
import sys
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from scripts.swarm_v2.kubernetes.storage import MountChecker, StorageRefused, normal
from scripts.swarm_v2.kubernetes.watch import EXECUTION_LABEL, OWNER_LABEL

SCHEMA = Path(__file__).resolve().parents[3] / "docs" / "swarm-v2" / "schemas" / "pod-policy.json"
DOMAIN = "swarm.agentihooks.io"
ATTEMPTS = "/home/worker/attempts"
LAUNCH_DIR = "/var/run/swarm/launch"
CREDENTIAL_DIR = "/var/run/swarm/credential"
WORKER_ID = 10001
TMP_MIB = 1024
PROBES = ("startup", "readiness", "liveness")
HARNESSES = ("claude", "codex")
LABEL_VALUE = re.compile(r"[A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,127}")
ACCOUNT = re.compile(r"[a-z0-9]([a-z0-9-]{0,46}[a-z0-9])?")
PROJECT = re.compile(r"github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+|local:[A-Za-z0-9._-]+|unknown")
IDENTITY = {
    "execution_id": re.compile(r"exe-[0-9a-f]{32}"),
    "task_id": LABEL_VALUE,
    "swarm_id": LABEL_VALUE,
    "seat_id": IDENTIFIER,
    "grant_ref": IDENTIFIER,
    "project_id": PROJECT,
    "provider_account": ACCOUNT,
}
COUNTS = ("generation", "controller_epoch")


class PodSpecRefused(ValueError):
    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class AdmittedLaunch:
    execution_id: str
    generation: int
    task_id: str
    swarm_id: str
    seat_id: str
    grant_ref: str
    controller_epoch: int
    project_id: str
    harness: str
    image_digest: str
    profile: str
    memory_mib: int
    cpu_millis: int
    provider_account: str
    task_payload: object

    @classmethod
    def from_record(cls, record: object) -> "AdmittedLaunch":
        if not isinstance(record, Mapping):
            raise PodSpecRefused("launch must be a JSON object", "fields")
        names = [f.name for f in fields(cls)]
        if extra := sorted(set(record) - set(names)):
            raise PodSpecRefused(f"launch has unknown fields: {', '.join(extra)}", "fields")
        if missing := [name for name in names if name not in record]:
            raise PodSpecRefused(f"launch is missing fields: {', '.join(missing)}", "fields")
        return cls(**{name: record[name] for name in names})


@dataclass(frozen=True)
class Rendered:
    pod: dict
    digest: str


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def canonical_digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _read(path: str | Path) -> object:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        raise PodSpecRefused(f"{path} is not a readable JSON file", "input") from None


def _where(path) -> str:
    return "/".join(str(part) for part in path) or "the document root"


def load_policy(path: str | Path) -> dict:
    policy = _read(path)
    error = best_match(Draft202012Validator(json.loads(SCHEMA.read_text())).iter_errors(policy))
    if error is not None:
        raise PodSpecRefused(f"pod policy is invalid at {_where(error.absolute_path)}: {error.message}", "policy")
    probes = policy["probes"]
    budget = probes["herdr_timeout_seconds"] + probes["brain_timeout_seconds"]
    for name in PROBES:
        if probes[name]["timeout_seconds"] <= budget:
            raise PodSpecRefused(
                f"{name} probe timeout_seconds must exceed the herdr and brain timeouts together", "policy"
            )
    mounts = policy.get("mounts", [])
    for field, key in (("name", str), ("mount_path", normal)):
        if len({key(mount[field]) for mount in mounts}) < len(mounts):
            raise PodSpecRefused(f"pod policy mounts repeat a {field}", "policy")
    return policy


def _count(value: object) -> bool:
    return type(value) is int and value >= 1


def _identity(launch: AdmittedLaunch) -> None:
    for name, pattern in IDENTITY.items():
        value = getattr(launch, name)
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise PodSpecRefused(f"launch {name} is not a valid value", "identity")
    for name in COUNTS:
        if not _count(getattr(launch, name)):
            raise PodSpecRefused(f"launch {name} is not a valid value", "identity")
    if launch.harness not in HARNESSES:
        raise PodSpecRefused("launch harness must be claude or codex", "harness")
    if not isinstance(launch.image_digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", launch.image_digest):
        raise PodSpecRefused("launch image_digest must be a sha256 digest", "image")


def _profile(policy: dict, launch: AdmittedLaunch) -> dict:
    if not isinstance(launch.profile, str) or launch.profile not in policy["profiles"]:
        raise PodSpecRefused("launch profile is not an approved resource profile", "profile")
    limits = policy["profiles"][launch.profile]["limits"]
    if not (_count(launch.memory_mib) and _count(launch.cpu_millis)):
        raise PodSpecRefused("launch resources must be positive integers", "resources")
    if launch.memory_mib > limits["memory_mib"] or launch.cpu_millis > limits["cpu_millis"]:
        raise PodSpecRefused(f"launch resources exceed the {launch.profile} profile limits", "resources")
    if launch.provider_account not in policy["provider_accounts"]:
        raise PodSpecRefused("launch provider_account is not an approved provider account", "account")
    return policy["profiles"][launch.profile]


def _payload(launch: AdmittedLaunch) -> str:
    if not isinstance(launch.task_payload, Mapping):
        raise PodSpecRefused("launch task_payload must be a JSON object", "payload")
    try:
        return canonical_digest(launch.task_payload)
    except (TypeError, ValueError):
        raise PodSpecRefused("launch task_payload must be a JSON object", "payload") from None


def _metadata(policy: dict, launch: AdmittedLaunch, payload: str) -> dict:
    version = policy["template_version"]
    return {
        "name": f"swarm-{launch.execution_id}",
        "namespace": policy["namespace"],
        "labels": {
            "app.kubernetes.io/managed-by": "agentihooks",
            "app.kubernetes.io/component": "swarm-execution",
            OWNER_LABEL: policy["owner"],
            EXECUTION_LABEL: launch.execution_id,
            f"{DOMAIN}/generation": str(launch.generation),
            f"{DOMAIN}/swarm": launch.swarm_id,
            f"{DOMAIN}/task": launch.task_id,
            f"{DOMAIN}/template-version": version,
            f"{DOMAIN}/provider-account": launch.provider_account,
        },
        "annotations": {
            f"{DOMAIN}/seat": launch.seat_id,
            f"{DOMAIN}/grant-ref": launch.grant_ref,
            f"{DOMAIN}/controller-epoch": str(launch.controller_epoch),
            f"{DOMAIN}/project": launch.project_id,
            f"{DOMAIN}/harness": launch.harness,
            f"{DOMAIN}/profile": launch.profile,
            f"{DOMAIN}/image-digest": launch.image_digest,
            f"{DOMAIN}/template-version": version,
            f"{DOMAIN}/task-payload-digest": payload,
        },
    }


def _seconds(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def _probe(policy: dict, launch: AdmittedLaunch, mode: str) -> dict:
    probes = policy["probes"]
    command = ["python", "/opt/swarm-node/health.py", mode, "--attempt", f"{ATTEMPTS}/{launch.execution_id}"]
    command += ["--harness", launch.harness, "--herdr-timeout", _seconds(probes["herdr_timeout_seconds"])]
    command += ["--brain-timeout", _seconds(probes["brain_timeout_seconds"])]
    timing = probes[mode]
    return {
        "exec": {"command": command},
        "initialDelaySeconds": timing["initial_delay_seconds"],
        "periodSeconds": timing["period_seconds"],
        "timeoutSeconds": timing["timeout_seconds"],
        "failureThreshold": timing["failure_threshold"],
    }


def _resources(launch: AdmittedLaunch, limits: dict) -> dict:
    disk = f"{limits['ephemeral_mib']}Mi"
    return {
        "requests": {"cpu": f"{launch.cpu_millis}m", "memory": f"{launch.memory_mib}Mi", "ephemeral-storage": disk},
        "limits": {"cpu": f"{limits['cpu_millis']}m", "memory": f"{limits['memory_mib']}Mi", "ephemeral-storage": disk},
    }


def _read_only(mount: dict) -> bool:
    return mount["purpose"] != "artifact"


def _shared_volume(mount: dict) -> dict:
    source = mount["source"]
    if "claim" in source:
        return {"persistentVolumeClaim": {"claimName": source["claim"], "readOnly": _read_only(mount)}}
    if "host_path" in source:
        return {"hostPath": {"path": source["host_path"], "type": "Directory"}}
    return {"nfs": {**source["nfs"], "readOnly": _read_only(mount)}}


def _shared_volumes(policy: dict) -> list[dict]:
    return [{"name": f"shared-{mount['name']}", **_shared_volume(mount)} for mount in policy.get("mounts", [])]


def _shared_mounts(policy: dict, launch: AdmittedLaunch) -> list[dict]:
    mounts = []
    for mount in policy.get("mounts", []):
        at = {"name": f"shared-{mount['name']}", "mountPath": mount["mount_path"], "readOnly": _read_only(mount)}
        mounts.append(at if _read_only(mount) else at | {"subPath": launch.execution_id})
    return mounts


def _container(policy: dict, launch: AdmittedLaunch, profile: dict) -> dict:
    container = {
        "name": "agent",
        "image": f"{policy['image_repository']}@{launch.image_digest}",
        "imagePullPolicy": "IfNotPresent",
        "args": [
            "python",
            "/opt/swarm-node/supervisor.py",
            f"{ATTEMPTS}/{launch.execution_id}",
            f"{LAUNCH_DIR}/launch.json",
        ],
        "env": [{"name": "BRAIN_URL", "value": policy["brain_url"]}] if "brain_url" in policy else [],
        "resources": _resources(launch, profile["limits"]),
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "privileged": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        },
        "volumeMounts": [
            {"name": "home", "mountPath": "/home/worker"},
            {"name": "tmp", "mountPath": "/tmp"},  # NOSONAR: a Pod-private emptyDir, never the host /tmp
            {"name": "launch", "mountPath": LAUNCH_DIR, "readOnly": True},
            {"name": "credential", "mountPath": CREDENTIAL_DIR, "readOnly": True},
            *_shared_mounts(policy, launch),
        ],
    }
    for mode in PROBES:
        container[f"{mode}Probe"] = _probe(policy, launch, mode)
    return container


def _placement(profile: dict) -> dict:
    placement = {"nodeSelector": dict(profile["node_selector"]), "tolerations": copy.deepcopy(profile["tolerations"])}
    excluded = [
        {"key": key, "operator": "NotIn", "values": list(values)} for key, values in profile["excluded_nodes"].items()
    ]
    if excluded:
        terms = {"nodeSelectorTerms": [{"matchExpressions": excluded}]}
        placement["affinity"] = {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": terms}}
    return placement


def _spec(policy: dict, launch: AdmittedLaunch, profile: dict) -> dict:
    body = {
        "restartPolicy": "Never",
        "serviceAccountName": policy["service_account"],
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "hostNetwork": False,
        "hostPID": False,
        "hostIPC": False,
        "terminationGracePeriodSeconds": policy["termination_grace_seconds"],
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": WORKER_ID,
            "runAsGroup": WORKER_ID,
            "fsGroup": WORKER_ID,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        **_placement(profile),
        "containers": [_container(policy, launch, profile)],
        "volumes": [
            {"name": "home", "emptyDir": {"sizeLimit": f"{profile['limits']['ephemeral_mib'] - TMP_MIB}Mi"}},
            {"name": "tmp", "emptyDir": {"sizeLimit": f"{TMP_MIB}Mi"}},
            {"name": "launch", "configMap": {"name": f"swarm-{launch.execution_id}-launch", "defaultMode": 0o444}},
            {
                "name": "credential",
                "secret": {
                    "secretName": f"swarm-account-{launch.provider_account}",
                    "items": [{"key": "token", "path": "token"}],
                    "defaultMode": 0o400,
                },
            },
            *_shared_volumes(policy),
        ],
        "runtimeClassName": policy["runtime_class_name"],
    }
    return body


class PodTemplate:
    def __init__(self, policy: dict) -> None:
        self.policy = policy
        self.failures = Counter()
        self.storage = MountChecker()

    def render(self, record: object) -> Rendered:
        try:
            launch = AdmittedLaunch.from_record(record)
            _identity(launch)
            profile = _profile(self.policy, launch)
            payload = _payload(launch)
        except PodSpecRefused as refused:
            self.failures[refused.reason] += 1
            raise
        pod = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": _metadata(self.policy, launch, payload),
            "spec": _spec(self.policy, launch, profile),
        }
        try:
            self.storage.check(pod)
        except StorageRefused as refused:
            self.failures["storage"] += 1
            raise PodSpecRefused(str(refused), "storage") from None
        return Rendered(pod, canonical_digest(pod))

    def pod_spec_validation_failures_total(self) -> dict[str, int]:
        return dict(self.failures)

    def shared_runtime_mount_rejections_total(self) -> dict[str, int]:
        return self.storage.shared_runtime_mount_rejections_total()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.swarm_v2.kubernetes.spec")
    parser.add_argument("action", choices=("render", "digest"))
    parser.add_argument("--policy", required=True)
    parser.add_argument("--launch", required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 64
    try:
        rendered = PodTemplate(load_policy(args.policy)).render(_read(args.launch))
    except PodSpecRefused as refused:
        print(refused, file=sys.stderr)
        return 1
    print(json.dumps(rendered.pod, sort_keys=True) if args.action == "render" else rendered.digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
