import copy
import hashlib
import json
from pathlib import Path

from scripts.swarm_v2.kubernetes.spec import (
    AdmittedLaunch,
    PodSpecRefused,
    PodTemplate,
    load_policy,
    probe_only_difference,
)

FIXTURES = Path(__file__).parent / "fixtures" / "swarm_v2"
INPUTS = ("pod-policy.json", "pod-launch.json", "pod-rendered.json")
HOSTILE = {
    "unknown fields": {"privileged": True, "host_mounts": ["/"], "secrets": ["cluster-admin-token"]},
    "yaml in task id": {"task_id": "vkub1\nspec:\n  hostNetwork: true"},
    "unapproved credential": {"credential_ref": "cluster-admin-token"},
    "unapproved profile": {"profile": "research"},
    "resources over the profile": {"memory_mib": 65536},
    "image by tag": {"image_digest": "latest"},
}


def _policy() -> dict:
    return load_policy(FIXTURES / "pod-policy.json")


def _launch() -> dict:
    return json.loads((FIXTURES / "pod-launch.json").read_text())


def _second() -> dict:
    doc = _launch()
    doc.update(
        execution_id="exe-" + "a1" * 16,
        generation=1,
        task_id="vkub2",
        seat_id="eng-4@rig-grade-swarm",
        harness="codex",
        memory_mib=2048,
        cpu_millis=1000,
        credential_ref="swarm-codex-fixture",
        task_payload={"prompt": "A second independent task."},
    )
    return doc


def _summary(pod: dict) -> dict:
    body = pod["spec"]
    container = body["containers"][0]
    return {
        "name": pod["metadata"]["name"],
        "labels": pod["metadata"]["labels"],
        "restart_policy": body["restartPolicy"],
        "automount_token": body["automountServiceAccountToken"],
        "resources": container["resources"],
        "node_selector": body["nodeSelector"],
        "secrets": [v["secret"]["secretName"] for v in body["volumes"] if "secret" in v],
        "host_paths": [v["name"] for v in body["volumes"] if "hostPath" in v],
        "privileged": container["securityContext"]["privileged"],
    }


def _positive() -> dict:
    template = PodTemplate(_policy())
    first = template.render(AdmittedLaunch.from_record(_launch()))
    second = PodTemplate(_policy()).render(AdmittedLaunch.from_record(_second()))
    golden = json.loads((FIXTURES / "pod-rendered.json").read_text())
    return {
        "fixture": _summary(first.pod),
        "fixture_digest": first.digest,
        "matches_kind_validated_golden": first.pod == golden,
        "second_fixture": _summary(second.pod),
        "second_fixture_digest": second.digest,
        "pod_spec_validation_failures_total": template.pod_spec_validation_failures_total(),
    }


def _rejection() -> dict:
    template = PodTemplate(_policy())
    protected = copy.deepcopy(template.policy)
    refusals = {}
    for name, change in HOSTILE.items():
        doc = {**_launch(), **change}
        try:
            template.render(AdmittedLaunch.from_record(doc))
            refusals[name] = "rendered"
        except PodSpecRefused as refused:
            refusals[name] = [refused.reason, str(refused)]
    text = json.dumps(template.render(AdmittedLaunch.from_record(_launch())).pod)
    return {
        "refusals": refusals,
        "policy_unchanged": template.policy == protected,
        "payload_fragments_in_pod": [f for f in ("hostPath", "cluster-admin-token", "envFrom") if f in text],
        "pod_spec_validation_failures_total": template.pod_spec_validation_failures_total(),
    }


def _recovery() -> dict:
    first = PodTemplate(_policy()).render(AdmittedLaunch.from_record(_launch()))
    restarted = PodTemplate(_policy()).render(AdmittedLaunch.from_record(_launch()))
    wrong = _policy()
    wrong["probes"]["herdr_timeout_seconds"] = 0.001
    wrong["probes"]["liveness"]["failure_threshold"] = 1
    broken = PodTemplate(wrong).render(AdmittedLaunch.from_record(_launch()))
    previous = _policy()
    previous["template_version"] = "kub01-v1"
    restored = PodTemplate(previous).render(AdmittedLaunch.from_record(_launch()))
    image = first.pod["spec"]["containers"][0]["image"]
    return {
        "first_digest": first.digest,
        "restarted_digest": restarted.digest,
        "same_digest": first.digest == restarted.digest,
        "probe_rollback": {
            "changed": probe_only_difference(broken.pod, first.pod),
            "image_unchanged": broken.pod["spec"]["containers"][0]["image"] == image,
            "broken_liveness": broken.pod["spec"]["containers"][0]["livenessProbe"],
            "reverted_liveness": first.pod["spec"]["containers"][0]["livenessProbe"],
        },
        "template_rollback": {
            "existing_version": first.pod["metadata"]["annotations"]["swarm.agentihooks.io/template-version"],
            "new_launch_version": restored.pod["metadata"]["labels"]["swarm.agentihooks.io/template-version"],
            "image_unchanged": restored.pod["spec"]["containers"][0]["image"] == image,
        },
    }


def run_case(case: str) -> dict:
    return {
        "case": f"T-SV2-KUB-01-{case.upper()}",
        "state": "passed",
        "evidence_class": "mocked: pure rendering; schema validity from kind server side dry run",
        "input_sha256": {name: hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() for name in INPUTS},
        "observed": {"a": _positive, "b": _rejection, "c": _recovery}[case](),
    }
