import copy
import hashlib
import json
from pathlib import Path

from scripts.swarm_v2.kubernetes.spec import PROBES, PodSpecRefused, PodTemplate, load_policy

FIXTURES = Path(__file__).parent / "fixtures" / "swarm_v2"
INPUTS = ("pod-policy.json", "pod-launch.json", "pod-rendered.json", "pod-rendered-wrong-probes.json")
EVIDENCE_CLASS = (
    "mocked: in process rendering; Kubernetes schema validity comes from the kind job's strict server side "
    "dry run of the committed golden Pods, recorded with its run id in result.json"
)
HOSTILE = {
    "unknown fields": {"privileged": True, "host_mounts": ["/"], "secrets": ["cluster-admin-token"]},
    "yaml in task id": {"task_id": "vkub1\nspec:\n  hostNetwork: true"},
    "unapproved credential": {"credential_ref": "cluster-admin-token"},
    "unapproved profile": {"profile": "research"},
    "resources over the profile": {"memory_mib": 65536},
    "image by tag": {"image_digest": "latest"},
}
PROBE_PATHS = [f"spec.containers[0].{mode}Probe" for mode in sorted(PROBES)]


def differences(left: object, right: object, path: str = "") -> list[str]:
    if isinstance(left, dict) and isinstance(right, dict):
        found = []
        for key in sorted(set(left) | set(right)):
            found += differences(left.get(key), right.get(key), f"{path}.{key}" if path else key)
        return found
    if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
        found = []
        for index, (a, b) in enumerate(zip(left, right, strict=True)):
            found += differences(a, b, f"{path}[{index}]")
        return found
    return [] if left == right else [path]


def probe_only_difference(before: dict, after: dict) -> list[str] | None:
    """The differing probe paths when only probes differ; None when anything else changed."""
    changed = sorted({next((p for p in PROBE_PATHS if d.startswith(p)), d) for d in differences(before, after)})
    return changed if set(changed) <= set(PROBE_PATHS) else None


def policy() -> dict:
    return load_policy(FIXTURES / "pod-policy.json")


def wrong_probes() -> dict:
    wrong = policy()
    wrong["probes"]["herdr_timeout_seconds"] = 0.001
    wrong["probes"]["liveness"]["failure_threshold"] = 1
    return wrong


def launch() -> dict:
    return json.loads((FIXTURES / "pod-launch.json").read_text())


def second() -> dict:
    doc = launch()
    doc.update(
        execution_id="exe-" + "a1" * 16,
        generation=1,
        task_id="vkub2",
        seat_id="eng-4@rig-grade-swarm",
        harness="codex",
        memory_mib=2048,
        cpu_millis=1000,
        credential_ref="codex-fixture",
        task_payload={"prompt": "A second independent task."},
    )
    return doc


def _summary(pod: dict) -> dict:
    body = pod["spec"]
    container = body["containers"][0]
    return {
        "name": pod["metadata"]["name"],
        "labels": pod["metadata"]["labels"],
        "harness": pod["metadata"]["annotations"]["swarm.agentihooks.io/harness"],
        "probe_harness": container["livenessProbe"]["exec"]["command"][6],
        "restart_policy": body["restartPolicy"],
        "automount_token": body["automountServiceAccountToken"],
        "resources": container["resources"],
        "node_selector": body["nodeSelector"],
        "secrets": [v["secret"]["secretName"] for v in body["volumes"] if "secret" in v],
        "host_paths": [v["name"] for v in body["volumes"] if "hostPath" in v],
        "privileged": container["securityContext"]["privileged"],
    }


def _positive() -> tuple[dict, bool]:
    template = PodTemplate(policy())
    first = template.render(launch())
    other = PodTemplate(policy()).render(second())
    golden = json.loads((FIXTURES / "pod-rendered.json").read_text())
    observed = {
        "fixture": _summary(first.pod),
        "fixture_digest": first.digest,
        "matches_golden_pod": first.pod == golden,
        "second_fixture": _summary(other.pod),
        "second_fixture_digest": other.digest,
        "pod_spec_validation_failures_total": template.pod_spec_validation_failures_total(),
    }
    checks = [
        observed["matches_golden_pod"],
        observed["second_fixture"]["harness"] == observed["second_fixture"]["probe_harness"] == "codex",
        first.pod["metadata"]["labels"]["swarm.agentihooks.io/execution-id"] == launch()["execution_id"],
        observed["pod_spec_validation_failures_total"] == {},
    ]
    return observed, all(checks)


def _rejection() -> tuple[dict, bool]:
    template = PodTemplate(policy())
    protected = copy.deepcopy(template.policy)
    refusals = {}
    for name, change in HOSTILE.items():
        try:
            template.render({**launch(), **change})
            refusals[name] = "rendered"
        except PodSpecRefused as refused:
            refusals[name] = [refused.reason, str(refused)]
    text = json.dumps(template.render(launch()).pod)
    observed = {
        "refusals": refusals,
        "policy_unchanged": template.policy == protected,
        "payload_fragments_in_pod": [f for f in ("hostPath", "cluster-admin-token", "envFrom") if f in text],
        "pod_spec_validation_failures_total": template.pod_spec_validation_failures_total(),
    }
    checks = [
        "rendered" not in refusals.values(),
        observed["policy_unchanged"],
        observed["payload_fragments_in_pod"] == [],
        sum(observed["pod_spec_validation_failures_total"].values()) == len(HOSTILE),
    ]
    return observed, all(checks)


def _recovery() -> tuple[dict, bool]:
    first = PodTemplate(policy()).render(launch())
    restarted = PodTemplate(policy()).render(launch())
    broken = PodTemplate(wrong_probes()).render(launch())
    previous = policy()
    previous["template_version"] = "kub01-v1"
    restored = PodTemplate(previous).render(launch())
    image = first.pod["spec"]["containers"][0]["image"]
    golden = json.loads((FIXTURES / "pod-rendered-wrong-probes.json").read_text())
    observed = {
        "first_digest": first.digest,
        "restarted_digest": restarted.digest,
        "same_digest": first.digest == restarted.digest,
        "probe_rollback": {
            "evidence_class": "render level: the wrong and reverted Pods differ only in their probes",
            "changed": probe_only_difference(broken.pod, first.pod),
            "image_unchanged": broken.pod["spec"]["containers"][0]["image"] == image,
            "broken_matches_golden": broken.pod == golden,
            "broken_liveness": broken.pod["spec"]["containers"][0]["livenessProbe"],
            "reverted_liveness": first.pod["spec"]["containers"][0]["livenessProbe"],
        },
        "template_rollback": {
            "existing_version": first.pod["metadata"]["annotations"]["swarm.agentihooks.io/template-version"],
            "new_launch_version": restored.pod["metadata"]["labels"]["swarm.agentihooks.io/template-version"],
            "image_unchanged": restored.pod["spec"]["containers"][0]["image"] == image,
        },
    }
    rollback = observed["probe_rollback"]
    checks = [
        observed["same_digest"],
        rollback["changed"] == PROBE_PATHS,
        rollback["image_unchanged"],
        rollback["broken_matches_golden"],
        observed["template_rollback"]["existing_version"] == "kub01-v2",
        observed["template_rollback"]["new_launch_version"] == "kub01-v1",
    ]
    return observed, all(checks)


def run_case(case: str) -> dict:
    observed, passed = {"a": _positive, "b": _rejection, "c": _recovery}[case]()
    return {
        "case": f"T-SV2-KUB-01-{case.upper()}",
        "state": "passed" if passed else "failed",
        "evidence_class": EVIDENCE_CLASS,
        "input_sha256": {name: hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() for name in INPUTS},
        "observed": observed,
    }
