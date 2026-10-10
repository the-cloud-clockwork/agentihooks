import copy
import json
from pathlib import Path

import pytest

from scripts.swarm_v2.kubernetes import spec
from scripts.swarm_v2.kubernetes.spec import PodSpecRefused, PodTemplate, canonical_digest, load_policy
from scripts.swarm_v2.kubernetes.watch import EXECUTION_LABEL, OWNER_LABEL
from tests import sv2_kub01_cases as cases

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures" / "swarm_v2"
POLICY = FIXTURES / "pod-policy.json"
LAUNCH = FIXTURES / "pod-launch.json"
EVIDENCE = Path(__file__).parents[1] / "evidence" / "SV2-KUB-01"
EXECUTION = "exe-0f1e2d3c4b5a69788796a5b4c3d2e1f0"
ATTEMPT = f"/home/worker/attempts/{EXECUTION}"
TOKEN_ENV = {
    "name": "AH_CC_TOKEN_claude_fixtureatexample_com",
    "valueFrom": {"secretKeyRef": {"name": "swarm-claude-creds", "key": "claude-fixtureatexample-com"}},
}


def policy_doc() -> dict:
    return json.loads(POLICY.read_text())


def launch_doc() -> dict:
    return json.loads(LAUNCH.read_text())


def write(tmp_path, doc, name="policy.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(doc))
    return path


def render(launch: dict | None = None, policy: dict | None = None):
    template = PodTemplate(policy or load_policy(POLICY))
    return template, template.render(launch or launch_doc())


def health(mode: str, herdr: str = "2", brain: str = "2") -> list:
    return [
        "python",
        "/opt/swarm-node/health.py",
        mode,
        "--attempt",
        ATTEMPT,
        "--harness",
        "claude",
        "--herdr-timeout",
        herdr,
        "--brain-timeout",
        brain,
    ]


def test_the_fixture_renders_a_pod_bound_to_its_launch_identity():
    _, rendered = render()
    meta = rendered.pod["metadata"]
    assert rendered.pod["apiVersion"] == "v1"
    assert rendered.pod["kind"] == "Pod"
    assert meta["name"] == f"swarm-{EXECUTION}"
    assert meta["namespace"] == "swarm-pod-proof"
    assert meta["labels"] == {
        "app.kubernetes.io/managed-by": "agentihooks",
        "app.kubernetes.io/component": "swarm-execution",
        OWNER_LABEL: "agentihooks-swarm-fixture",
        EXECUTION_LABEL: EXECUTION,
        "swarm.agentihooks.io/generation": "3",
        "swarm.agentihooks.io/swarm": "rig-grade-swarm",
        "swarm.agentihooks.io/task": "vkub1",
        "swarm.agentihooks.io/template-version": "kub01-v2",
        "swarm.agentihooks.io/provider-account": "claude-fixtureatexample-com",
    }
    assert meta["annotations"] == {
        "swarm.agentihooks.io/seat": "eng-3@rig-grade-swarm",
        "swarm.agentihooks.io/grant-ref": "lgr-00112233445566778899aabbccddeeff",
        "swarm.agentihooks.io/controller-epoch": "7",
        "swarm.agentihooks.io/project": "github.com/the-cloud-clockwork/agentihooks",
        "swarm.agentihooks.io/harness": "claude",
        "swarm.agentihooks.io/profile": "general",
        "swarm.agentihooks.io/image-digest": "sha256:" + "4b" * 32,
        "swarm.agentihooks.io/template-version": "kub01-v2",
        "swarm.agentihooks.io/task-payload-digest": canonical_digest(launch_doc()["task_payload"]),
    }


def test_the_single_agent_container_runs_the_supervisor_from_the_admitted_image():
    _, rendered = render()
    (container,) = rendered.pod["spec"]["containers"]
    assert container["name"] == "agent"
    assert container["image"] == "ghcr.io/the-cloud-clockwork/agentihooks-worker@sha256:" + "4b" * 32
    assert container["imagePullPolicy"] == "IfNotPresent"
    assert "command" not in container
    assert container["args"] == [
        "python",
        "/opt/swarm-node/supervisor.py",
        ATTEMPT,
        "/var/run/swarm/launch/launch.json",
    ]
    assert container["env"] == [TOKEN_ENV, {"name": "BRAIN_URL", "value": "http://brain-api.swarm-brain.svc:8080"}]
    assert "initContainers" not in rendered.pod["spec"]
    assert "ephemeralContainers" not in rendered.pod["spec"]


def test_a_policy_without_a_brain_sets_only_the_account_token():
    policy = load_policy(POLICY)
    del policy["brain_url"]
    _, rendered = render(policy=policy)
    assert rendered.pod["spec"]["containers"][0]["env"] == [TOKEN_ENV]


def test_requests_come_from_the_admitted_launch_and_limits_from_the_profile():
    _, rendered = render()
    assert rendered.pod["spec"]["containers"][0]["resources"] == {
        "requests": {"cpu": "1500m", "memory": "3072Mi", "ephemeral-storage": "10240Mi"},
        "limits": {"cpu": "2000m", "memory": "4096Mi", "ephemeral-storage": "10240Mi"},
    }


def test_node_constraints_come_from_the_profile_and_exclude_its_pools():
    _, rendered = render()
    body = rendered.pod["spec"]
    assert body["nodeSelector"] == {"anton.io/pool": "general"}
    assert body["tolerations"] == [{"key": "anton.io/spot", "operator": "Exists", "effect": "NoSchedule"}]
    assert body["affinity"] == {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {"matchExpressions": [{"key": "anton.io/pool", "operator": "NotIn", "values": ["research"]}]}
                ]
            }
        }
    }


def test_a_profile_without_excluded_nodes_sets_no_affinity():
    policy = load_policy(POLICY)
    policy["profiles"]["general"]["excluded_nodes"] = {}
    _, rendered = render(policy=policy)
    assert "affinity" not in rendered.pod["spec"]


def test_the_worker_is_hardened_and_never_mounts_a_service_account_token():
    _, rendered = render()
    body = rendered.pod["spec"]
    assert body["restartPolicy"] == "Never"
    assert body["automountServiceAccountToken"] is False
    assert body["serviceAccountName"] == "swarm-worker"
    assert body["enableServiceLinks"] is False
    assert body["hostNetwork"] is False
    assert body["hostPID"] is False
    assert body["hostIPC"] is False
    assert body["terminationGracePeriodSeconds"] == 60
    assert body["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 10001,
        "runAsGroup": 10001,
        "fsGroup": 10001,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert body["containers"][0]["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "privileged": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }


def test_a_policy_without_a_runtime_class_is_refused_before_any_pod_renders(tmp_path):
    doc = policy_doc()
    doc.pop("runtime_class_name", None)
    with pytest.raises(PodSpecRefused) as refused:
        load_policy(write(tmp_path, doc))
    assert (
        str(refused.value) == "pod policy is invalid at the document root: 'runtime_class_name' is a required property"
    )
    assert refused.value.reason == "policy"


def test_the_policy_runtime_class_reaches_the_pod():
    assert render()[1].pod["spec"]["runtimeClassName"] == "kata-fc"
    policy = load_policy(POLICY)
    policy["runtime_class_name"] = "kata-qemu"
    _, rendered = render(policy=policy)
    assert rendered.pod["spec"]["runtimeClassName"] == "kata-qemu"


def test_volumes_are_private_scratch_and_the_launch_record_with_no_credential_mount():
    _, rendered = render()
    body = rendered.pod["spec"]
    assert body["volumes"] == [
        {"name": "home", "emptyDir": {"sizeLimit": "9216Mi"}},
        {"name": "tmp", "emptyDir": {"sizeLimit": "1024Mi"}},
        {"name": "launch", "configMap": {"name": f"swarm-{EXECUTION}-launch", "defaultMode": 0o444}},
    ]
    assert body["containers"][0]["volumeMounts"] == [
        {"name": "home", "mountPath": "/home/worker"},
        {"name": "tmp", "mountPath": "/tmp"},
        {"name": "launch", "mountPath": "/var/run/swarm/launch", "readOnly": True},
    ]


def test_probes_call_the_image_health_contract_with_the_policy_thresholds():
    _, rendered = render()
    container = rendered.pod["spec"]["containers"][0]
    assert container["startupProbe"] == {
        "exec": {"command": health("startup")},
        "initialDelaySeconds": 0,
        "periodSeconds": 5,
        "timeoutSeconds": 6,
        "failureThreshold": 24,
    }
    assert container["readinessProbe"] == {
        "exec": {"command": health("readiness")},
        "initialDelaySeconds": 0,
        "periodSeconds": 10,
        "timeoutSeconds": 6,
        "failureThreshold": 3,
    }
    assert container["livenessProbe"] == {
        "exec": {"command": health("liveness")},
        "initialDelaySeconds": 0,
        "periodSeconds": 15,
        "timeoutSeconds": 6,
        "failureThreshold": 4,
    }


def test_fractional_probe_timeouts_render_exactly():
    policy = load_policy(POLICY)
    policy["probes"]["herdr_timeout_seconds"] = 1.5
    policy["probes"]["brain_timeout_seconds"] = 0.25
    _, rendered = render(policy=policy)
    probe = rendered.pod["spec"]["containers"][0]["livenessProbe"]["exec"]["command"]
    assert probe == health("liveness", "1.5", "0.25")


def test_the_task_payload_reaches_the_pod_only_as_its_digest():
    benign = launch_doc()
    benign["task_payload"] = {"prompt": "Fix the flaky test."}
    _, hostile = render()
    _, plain = render(benign)
    text = json.dumps(hostile.pod)
    for fragment in ("hostPath", "cluster-admin-token", 'hostNetwork": true', 'privileged": true', "envFrom"):
        assert fragment not in text
    assert "Fix the flaky test" not in text
    assert "exe-ffffffffffffffffffffffffffffffff" not in text
    hostile_meta = copy.deepcopy(hostile.pod)
    plain_meta = copy.deepcopy(plain.pod)
    key = "swarm.agentihooks.io/task-payload-digest"
    hostile_digest = hostile_meta["metadata"]["annotations"].pop(key)
    plain_digest = plain_meta["metadata"]["annotations"].pop(key)
    assert hostile_digest != plain_digest
    assert hostile_meta == plain_meta


def test_the_payload_digest_is_canonical_json_sha256():
    assert canonical_digest({"b": 1, "a": "x"}) == canonical_digest({"a": "x", "b": 1})
    assert canonical_digest({"a": 1}) == "sha256:015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862"


REFUSALS = [
    ("extra field", lambda d: d.update(privileged=True), "launch has unknown fields: privileged", "fields"),
    (
        "two extra fields",
        lambda d: d.update(host_mounts=["/"], secrets=["cluster-admin-token"]),
        "launch has unknown fields: host_mounts, secrets",
        "fields",
    ),
    ("missing field", lambda d: d.pop("provider_account"), "launch is missing fields: provider_account", "fields"),
    (
        "yaml in task id",
        lambda d: d.update(task_id="vkub1\nspec:\n  hostNetwork: true"),
        "launch task_id is not a valid value",
        "identity",
    ),
    (
        "bad execution id",
        lambda d: d.update(execution_id="exe-XYZ"),
        "launch execution_id is not a valid value",
        "identity",
    ),
    ("long swarm id", lambda d: d.update(swarm_id="s" * 64), "launch swarm_id is not a valid value", "identity"),
    ("bad seat", lambda d: d.update(seat_id="eng 3"), "launch seat_id is not a valid value", "identity"),
    ("bad grant", lambda d: d.update(grant_ref="-grant"), "launch grant_ref is not a valid value", "identity"),
    ("bad project", lambda d: d.update(project_id="../etc"), "launch project_id is not a valid value", "identity"),
    ("zero generation", lambda d: d.update(generation=0), "launch generation is not a valid value", "identity"),
    ("bool generation", lambda d: d.update(generation=True), "launch generation is not a valid value", "identity"),
    (
        "text epoch",
        lambda d: d.update(controller_epoch="7"),
        "launch controller_epoch is not a valid value",
        "identity",
    ),
    ("unknown harness", lambda d: d.update(harness="copilot"), "launch harness must be claude or codex", "harness"),
    ("image tag", lambda d: d.update(image_digest="latest"), "launch image_digest must be a sha256 digest", "image"),
    (
        "unknown profile",
        lambda d: d.update(profile="research"),
        "launch profile is not an approved resource profile",
        "profile",
    ),
    (
        "memory over limit",
        lambda d: d.update(memory_mib=4097),
        "launch resources exceed the general profile limits",
        "resources",
    ),
    (
        "cpu over limit",
        lambda d: d.update(cpu_millis=2001),
        "launch resources exceed the general profile limits",
        "resources",
    ),
    ("zero memory", lambda d: d.update(memory_mib=0), "launch resources must be positive integers", "resources"),
    ("text cpu", lambda d: d.update(cpu_millis="1500"), "launch resources must be positive integers", "resources"),
    ("bool cpu", lambda d: d.update(cpu_millis=True), "launch resources must be positive integers", "resources"),
    (
        "unapproved provider account",
        lambda d: d.update(provider_account="cluster-admin@token.io"),
        "launch provider_account is not an approved provider account",
        "account",
    ),
    (
        "provider account without an at sign",
        lambda d: d.update(provider_account="claude-fixture"),
        "launch provider_account is not a valid value",
        "identity",
    ),
    (
        "provider account ending in a dash",
        lambda d: d.update(provider_account="claude-fixture@example.com-"),
        "launch provider_account is not a valid value",
        "identity",
    ),
    (
        "provider account with a line break",
        lambda d: d.update(provider_account="claude-fixture@example.com\n"),
        "launch provider_account is not a valid value",
        "identity",
    ),
    (
        "provider account past the label bound",
        lambda d: d.update(provider_account="a" * 58 + "@x.io"),
        "launch provider_account is not a valid value",
        "identity",
    ),
    ("list payload", lambda d: d.update(task_payload=["x"]), "launch task_payload must be a JSON object", "payload"),
    (
        "unserializable payload",
        lambda d: d.update(task_payload={1: "a", "b": 2}),
        "launch task_payload must be a JSON object",
        "payload",
    ),
    (
        "not a number payload",
        lambda d: d.update(task_payload={"x": float("nan")}),
        "launch task_payload must be a JSON object",
        "payload",
    ),
    ("numeric image", lambda d: d.update(image_digest=5), "launch image_digest must be a sha256 digest", "image"),
    (
        "numeric execution id",
        lambda d: d.update(execution_id=5),
        "launch execution_id is not a valid value",
        "identity",
    ),
    (
        "listed profile",
        lambda d: d.update(profile=["general"]),
        "launch profile is not an approved resource profile",
        "profile",
    ),
    ("long task id", lambda d: d.update(task_id="t" * 64), "launch task_id is not a valid value", "identity"),
]


@pytest.mark.parametrize(("change", "message", "reason"), [r[1:] for r in REFUSALS], ids=[r[0] for r in REFUSALS])
def test_hostile_launches_are_refused_without_output_and_counted(change, message, reason):
    template = PodTemplate(load_policy(POLICY))
    doc = launch_doc()
    change(doc)
    before = copy.deepcopy(template.policy)
    with pytest.raises(PodSpecRefused) as refused:
        template.render(doc)
    assert str(refused.value) == message
    assert refused.value.reason == reason
    assert template.pod_spec_validation_failures_total() == {reason: 1}
    assert template.policy == before
    assert "cluster-admin-token" not in message


def test_a_record_that_is_not_an_object_is_refused():
    template = PodTemplate(load_policy(POLICY))
    with pytest.raises(PodSpecRefused) as refused:
        template.render(["not", "a", "record"])
    assert str(refused.value) == "launch must be a JSON object"
    assert template.pod_spec_validation_failures_total() == {"fields": 1}


def test_failures_accumulate_per_reason_and_success_counts_nothing():
    template = PodTemplate(load_policy(POLICY))
    template.render(launch_doc())
    assert template.pod_spec_validation_failures_total() == {}
    for profile in ("research", "memory"):
        doc = launch_doc()
        doc["profile"] = profile
        with pytest.raises(PodSpecRefused):
            template.render(doc)
    doc = launch_doc()
    doc["harness"] = "copilot"
    with pytest.raises(PodSpecRefused):
        template.render(doc)
    assert template.pod_spec_validation_failures_total() == {"profile": 2, "harness": 1}


def test_rendering_the_same_admitted_launch_twice_gives_the_same_digest():
    _, first = render()
    _, second = render()
    assert first.digest == second.digest
    assert first.pod == second.pod
    assert first.digest == canonical_digest(first.pod)
    assert first.digest.startswith("sha256:")
    assert len(first.digest) == 71


def test_policy_key_order_does_not_change_the_digest(tmp_path):
    reordered = dict(reversed(list(policy_doc().items())))
    _, first = render()
    _, second = render(policy=load_policy(write(tmp_path, reordered)))
    assert first.digest == second.digest


def test_a_new_generation_or_payload_changes_the_digest():
    _, first = render()
    doc = launch_doc()
    doc["generation"] = 4
    _, newer = render(doc)
    doc = launch_doc()
    doc["task_payload"] = {"prompt": "other"}
    _, other = render(doc)
    assert len({first.digest, newer.digest, other.digest}) == 3


def test_the_canonical_digest_ignores_key_order_only():
    assert canonical_digest({"a": 1, "b": [1, 2]}) == canonical_digest({"b": [1, 2], "a": 1})
    assert canonical_digest({"a": 1, "b": [1, 2]}) != canonical_digest({"a": 1, "b": [2, 1]})
    assert canonical_digest({"a": 1}) == "sha256:015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862"


def test_render_does_not_mutate_the_launch_or_policy():
    policy = load_policy(POLICY)
    snapshot = copy.deepcopy(policy)
    launch = launch_doc()
    before = copy.deepcopy(launch)
    rendered = PodTemplate(policy).render(launch)
    rendered.pod["spec"]["nodeSelector"]["x"] = "y"
    rendered.pod["spec"]["tolerations"][0]["key"] = "changed"
    rendered.pod["spec"]["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][
        "nodeSelectorTerms"
    ][0]["matchExpressions"][0]["values"].append("general")
    assert policy == snapshot
    assert launch == before


def test_a_payload_that_refers_to_itself_is_refused():
    template = PodTemplate(load_policy(POLICY))
    doc = launch_doc()
    loop = {}
    loop["self"] = loop
    doc["task_payload"] = loop
    with pytest.raises(PodSpecRefused) as refused:
        template.render(doc)
    assert str(refused.value) == "launch task_payload must be a JSON object"


def test_resources_at_the_profile_limits_are_accepted():
    doc = launch_doc()
    doc.update(memory_mib=4096, cpu_millis=2000)
    _, rendered = render(doc)
    assert rendered.pod["spec"]["containers"][0]["resources"]["requests"] == {
        "cpu": "2000m",
        "memory": "4096Mi",
        "ephemeral-storage": "10240Mi",
    }


def test_identity_values_at_their_bounds_are_accepted():
    doc = launch_doc()
    doc.update(task_id="t" * 63, swarm_id="s" * 63, project_id="local:scratch")
    _, rendered = render(doc)
    assert rendered.pod["metadata"]["labels"]["swarm.agentihooks.io/task"] == "t" * 63
    doc["project_id"] = "unknown"
    _, rendered = render(doc)
    assert rendered.pod["metadata"]["annotations"]["swarm.agentihooks.io/project"] == "unknown"


def token_env(pod: dict) -> list:
    return [entry for entry in pod["spec"]["containers"][0]["env"] if "valueFrom" in entry]


def test_a_provider_account_at_its_bound_labels_and_reads_its_own_secret_key():
    email = "a" * 57 + "@x.io"
    policy = load_policy(POLICY)
    policy["provider_accounts"] = [email]
    doc = launch_doc()
    doc["provider_account"] = email
    _, rendered = render(doc, policy)
    key = "a" * 57 + "atx-io"
    assert len(key) == 63
    assert rendered.pod["metadata"]["labels"]["swarm.agentihooks.io/provider-account"] == key
    assert token_env(rendered.pod) == [
        {
            "name": "AH_CC_TOKEN_" + "a" * 57 + "atx_io",
            "valueFrom": {"secretKeyRef": {"name": "swarm-claude-creds", "key": key}},
        }
    ]


def test_an_upper_case_account_renders_the_same_pod_as_its_lower_case_form():
    doc = launch_doc()
    doc["provider_account"] = "CLAUDE-FIXTURE@EXAMPLE.COM"
    assert render(doc)[1].pod == render()[1].pod
    policy = load_policy(POLICY)
    policy["provider_accounts"] = ["Claude-Fixture@Example.com"]
    assert render(policy=policy)[1].pod == render()[1].pod


def test_a_codex_launch_names_codex_in_its_probes_and_binds_its_own_provider_account():
    doc = launch_doc()
    doc.update(harness="codex", provider_account="codex-fixture@example.com")
    _, rendered = render(doc)
    container = rendered.pod["spec"]["containers"][0]
    assert container["readinessProbe"]["exec"]["command"][5:7] == ["--harness", "codex"]
    assert rendered.pod["metadata"]["annotations"]["swarm.agentihooks.io/harness"] == "codex"
    assert rendered.pod["metadata"]["labels"]["swarm.agentihooks.io/provider-account"] == "codex-fixtureatexample-com"
    assert token_env(rendered.pod) == [
        {
            "name": "AH_CC_TOKEN_codex_fixtureatexample_com",
            "valueFrom": {"secretKeyRef": {"name": "swarm-claude-creds", "key": "codex-fixtureatexample-com"}},
        }
    ]


def test_a_probe_initial_delay_comes_from_the_policy():
    policy = load_policy(POLICY)
    policy["probes"]["startup"]["initial_delay_seconds"] = 3
    _, rendered = render(policy=policy)
    assert rendered.pod["spec"]["containers"][0]["startupProbe"]["initialDelaySeconds"] == 3


def test_the_fixture_policy_loads():
    assert load_policy(POLICY) == policy_doc()


POLICY_REFUSALS = [
    (
        "unknown top field",
        lambda d: d.update(host_network=True),
        "pod policy is invalid at the document root: Additional properties are not allowed ('host_network' was unexpected)",
    ),
    (
        "privileged in profile",
        lambda d: d["profiles"]["general"].update(privileged=True),
        "pod policy is invalid at profiles/general: Additional properties are not allowed ('privileged' was unexpected)",
    ),
    (
        "image with tag",
        lambda d: d.update(image_repository="ghcr.io/x/worker:dev"),
        "pod policy is invalid at image_repository: 'ghcr.io/x/worker:dev' does not match "
        "'^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]{1,5})?(/[a-z0-9]+([._-][a-z0-9]+)*)+$'",
    ),
    (
        "account name past the label bound",
        lambda d: d.update(provider_accounts=["a" * 58 + "@x.io"]),
        "pod policy is invalid at provider_accounts/0: '" + "a" * 58 + "@x.io' is too long",
    ),
    (
        "account name without an at sign",
        lambda d: d.update(provider_accounts=["claude-fixture"]),
        "pod policy is invalid at provider_accounts/0: 'claude-fixture' does not match "
        "'^[A-Za-z0-9]([A-Za-z0-9._+-]*[A-Za-z0-9])?@[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$'",
    ),
    (
        "unbounded grace",
        lambda d: d.update(termination_grace_seconds=901),
        "pod policy is invalid at termination_grace_seconds: 901 is greater than the maximum of 900",
    ),
    (
        "disk below the tmp allotment",
        lambda d: d["profiles"]["general"]["limits"].update(ephemeral_mib=2047),
        "pod policy is invalid at profiles/general/limits/ephemeral_mib: 2047 is less than the minimum of 2048",
    ),
    (
        "short readiness timeout",
        lambda d: d["probes"]["readiness"].update(timeout_seconds=4),
        "readiness probe timeout_seconds must exceed the herdr and brain timeouts together",
    ),
    (
        "short startup timeout",
        lambda d: d["probes"]["startup"].update(timeout_seconds=3),
        "startup probe timeout_seconds must exceed the herdr and brain timeouts together",
    ),
    (
        "short liveness timeout",
        lambda d: d["probes"]["liveness"].update(timeout_seconds=4),
        "liveness probe timeout_seconds must exceed the herdr and brain timeouts together",
    ),
]


@pytest.mark.parametrize(("change", "message"), [r[1:] for r in POLICY_REFUSALS], ids=[r[0] for r in POLICY_REFUSALS])
def test_an_invalid_policy_is_refused(tmp_path, change, message):
    doc = policy_doc()
    change(doc)
    with pytest.raises(PodSpecRefused) as refused:
        load_policy(write(tmp_path, doc))
    assert str(refused.value) == message
    assert refused.value.reason == "policy"


def test_a_probe_timeout_just_above_both_timeouts_is_accepted(tmp_path):
    doc = policy_doc()
    doc["probes"]["herdr_timeout_seconds"] = 2.5
    doc["probes"]["brain_timeout_seconds"] = 2.5
    for name in ("startup", "readiness", "liveness"):
        doc["probes"][name]["timeout_seconds"] = 6
    assert load_policy(write(tmp_path, doc))["probes"]["herdr_timeout_seconds"] == 2.5
    doc["probes"]["liveness"]["timeout_seconds"] = 5
    with pytest.raises(PodSpecRefused):
        load_policy(write(tmp_path, doc))


def test_restoring_the_previous_template_version_keeps_existing_pod_annotations():
    current = load_policy(POLICY)
    previous = copy.deepcopy(current)
    previous["template_version"] = "kub01-v1"
    previous["termination_grace_seconds"] = 30
    _, existing = render(policy=current)
    _, restored = render(policy=previous)
    assert existing.pod["metadata"]["annotations"]["swarm.agentihooks.io/template-version"] == "kub01-v2"
    assert restored.pod["metadata"]["labels"]["swarm.agentihooks.io/template-version"] == "kub01-v1"
    assert restored.pod["metadata"]["annotations"]["swarm.agentihooks.io/template-version"] == "kub01-v1"
    assert restored.pod["spec"]["terminationGracePeriodSeconds"] == 30
    assert restored.digest != existing.digest


def test_a_wrong_probe_threshold_reverts_alone_while_the_image_stays():
    _, broken = render(policy=cases.wrong_probes())
    _, reverted = render()
    assert cases.probe_only_difference(broken.pod, reverted.pod) == [
        "spec.containers[0].livenessProbe",
        "spec.containers[0].readinessProbe",
        "spec.containers[0].startupProbe",
    ]
    assert broken.pod["spec"]["containers"][0]["image"] == reverted.pod["spec"]["containers"][0]["image"]


def test_a_difference_outside_the_probes_is_reported():
    _, first = render()
    doc = launch_doc()
    doc["image_digest"] = "sha256:" + "5c" * 32
    _, second = render(doc)
    assert cases.probe_only_difference(first.pod, second.pod) is None
    assert cases.probe_only_difference(first.pod, first.pod) == []
    assert cases.differences({"a": [1, 2]}, {"a": [1]}) == ["a"]
    assert cases.differences({"a": 1}, {"b": 1}) == ["a", "b"]


def test_the_command_line_renders_the_pod_as_sorted_json(capsys):
    assert spec.main(["render", "--policy", str(POLICY), "--launch", str(LAUNCH)]) == 0
    assert capsys.readouterr().out == json.dumps(render()[1].pod, sort_keys=True) + "\n"


def test_the_command_line_refuses_a_hostile_launch_without_output(tmp_path, capsys):
    doc = launch_doc()
    doc["provider_account"] = "cluster-admin@token.io"
    assert spec.main(["render", "--policy", str(POLICY), "--launch", str(write(tmp_path, doc, "l.json"))]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "launch provider_account is not an approved provider account\n"


def test_the_command_line_refuses_unreadable_inputs(tmp_path, capsys):
    missing = tmp_path / "missing.json"
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert spec.main(["render", "--policy", str(missing), "--launch", str(LAUNCH)]) == 1
    assert capsys.readouterr().err == f"{missing} is not a readable JSON file\n"
    assert spec.main(["render", "--policy", str(POLICY), "--launch", str(broken)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"{broken} is not a readable JSON file\n"


def test_an_unreadable_policy_is_refused_as_input(tmp_path):
    missing = tmp_path / "missing.json"
    with pytest.raises(PodSpecRefused) as refused:
        load_policy(missing)
    assert str(refused.value) == f"{missing} is not a readable JSON file"
    assert refused.value.reason == "input"
    assert refused.value.__cause__ is None
    assert refused.value.__suppress_context__ is True


def test_the_command_line_digest_action_prints_the_spec_digest(capsys):
    assert spec.main(["digest", "--policy", str(POLICY), "--launch", str(LAUNCH)]) == 0
    assert capsys.readouterr().out == render()[1].digest + "\n"


def test_the_command_line_refuses_an_unknown_action(capsys):
    assert spec.main(["apply", "--policy", str(POLICY), "--launch", str(LAUNCH)]) == 64
    assert " ".join(capsys.readouterr().err.split()).startswith(
        "usage: python -m scripts.swarm_v2.kubernetes.spec [-h] --policy POLICY --launch LAUNCH {render,digest}"
    )


@pytest.mark.parametrize("missing", ["--policy", "--launch"])
def test_the_command_line_requires_both_inputs(missing):
    argv = ["render", "--policy", str(POLICY), "--launch", str(LAUNCH)]
    index = argv.index(missing)
    del argv[index : index + 2]
    assert spec.main(argv) == 64


def test_the_command_line_help_exits_cleanly(capsys):
    assert spec.main(["--help"]) == 0
    assert "--policy" in capsys.readouterr().out


def test_the_golden_pods_are_the_current_render():
    assert json.loads((FIXTURES / "pod-rendered.json").read_text()) == render()[1].pod
    wrong = json.loads((FIXTURES / "pod-rendered-wrong-probes.json").read_text())
    assert wrong == render(policy=cases.wrong_probes())[1].pod


def test_a_toleration_with_exists_and_a_value_is_refused(tmp_path):
    doc = policy_doc()
    doc["profiles"]["general"]["tolerations"][0]["value"] = "yes"
    with pytest.raises(PodSpecRefused) as refused:
        load_policy(write(tmp_path, doc))
    assert str(refused.value) == (
        "pod policy is invalid at profiles/general/tolerations/0: "
        "{'key': 'anton.io/spot', 'operator': 'Exists', 'effect': 'NoSchedule', 'value': 'yes'} "
        "should not be valid under {'required': ['value']}"
    )


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    from tests.sv2_kub01_cases import run_case

    first, second = run_case(case), run_case(case)
    assert first == second
    committed = json.loads((EVIDENCE / f"{case}-result.json").read_text())
    assert first["state"] == "passed"
    assert committed == first, json.dumps(first, indent=2, sort_keys=True)
