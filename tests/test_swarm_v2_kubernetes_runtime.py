import json
from pathlib import Path

import pytest

from scripts.swarm_v2.controller import FencedTransport
from scripts.swarm_v2.kubernetes import adoption, runtime, watch
from scripts.swarm_v2.kubernetes.client import AlreadyExists, ApiRefused
from scripts.swarm_v2.kubernetes.runtime import KubernetesTransport, PodStatus, pod_status
from scripts.swarm_v2.kubernetes.spec import PodTemplate
from scripts.swarm_v2.runtime.operations import Observation, Operation, Phase
from tests import sv2_kub02_cases as cases

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

EVIDENCE = Path(__file__).parents[1] / "evidence" / "SV2-KUB-02"
EXECUTION = "exe-0f1e2d3c4b5a69788796a5b4c3d2e1f0"
NAME = f"swarm-{EXECUTION}"
OWNER = "agentihooks-swarm-fixture"
DIGEST = "payload-digest"
ZERO = {"created": 0, "adopted": 0, "observed": 0, "quarantined": 0, "disabled": 0}


def journaled(launch=None) -> str:
    rendered = PodTemplate(cases.policy()).render(launch or cases.launch())
    return adoption.identity(rendered.digest, rendered.pod["spec"])


def operation(
    action="spawn", generation=3, execution_id=EXECUTION, payload_digest=DIGEST, spec_digest=None
) -> Operation:
    spec = journaled() if spec_digest is None else spec_digest
    return Operation("op-1", execution_id, generation, action, "kubernetes", payload_digest, {}, spec_digest=spec)


def transport(api=None, slug="fixture", creation_enabled=True) -> KubernetesTransport:
    api = api or cases.ApiServer(cases.policy()["namespace"])
    return KubernetesTransport(api, slug, PodTemplate(cases.policy()), creation_enabled)


def applied(execution_id=EXECUTION, generation=3, uid="uid-1", name=NAME) -> Observation:
    result = {"uid": uid, "name": name, "namespace": "swarm-pod-proof"}
    return Observation(Phase.APPLIED, execution_id, generation, "kubernetes", DIGEST, result)


def test_names_and_labels_are_fixed():
    assert runtime.GENERATION_LABEL == "swarm.agentihooks.io/generation"
    assert runtime.SPEC_DIGEST == "swarm.agentihooks.io/spec-digest"
    assert runtime.OPERATION_DIGEST == "swarm.agentihooks.io/operation-digest"
    assert KubernetesTransport.backend == "kubernetes"


def test_apply_creates_the_labelled_pod_and_returns_its_uid():
    sender = transport()
    assert sender.apply_operation(operation(), cases.launch()) == applied()
    pod = sender.api.objects[NAME]
    labels, notes = pod["metadata"]["labels"], pod["metadata"]["annotations"]
    assert labels[watch.OWNER_LABEL] == OWNER
    assert labels[watch.EXECUTION_LABEL] == EXECUTION
    assert notes[runtime.OPERATION_DIGEST] == DIGEST
    assert notes[runtime.SPEC_DIGEST] == PodTemplate(cases.policy()).render(cases.launch()).digest
    assert notes["swarm.agentihooks.io/seat"] == "eng-3@rig-grade-swarm"
    assert sender.kubernetes_create_reconciliation_total() == {**ZERO, "created": 1}
    assert sender.quarantined == {}


def test_a_created_pod_is_listed_by_the_pod_view_and_matched_by_the_reconciler():
    sender = transport()
    sender.apply_operation(operation(), cases.launch())
    view = watch.PodView(cases.Source(sender.api)).sync()
    plan = watch.Reconciler(watch.owner_for("fixture"), True).plan([EXECUTION], view.pods())
    assert plan.matched == {EXECUTION: "uid-1"}
    assert plan.counts()["foreign"] == plan.counts()["ambiguous"] == 0


@pytest.mark.parametrize("action", ["command", "drain", "terminate", "recover"])
def test_only_spawn_is_carried(action):
    sender = transport()
    assert sender.apply_operation(operation(action), cases.launch()) == Observation(Phase.REFUSED)
    assert sender.observe_operation(operation(action)) == Observation(Phase.REFUSED)
    assert sender.api.create_calls == 0


def test_disabled_creation_calls_nothing_and_counts_each_hold():
    sender = transport(creation_enabled=False)
    assert sender.apply_operation(operation(), cases.launch()) == Observation(Phase.ABSENT)
    sender.apply_operation(operation(), cases.launch())
    assert sender.api.create_calls == 0
    assert sender.kubernetes_create_reconciliation_total() == {**ZERO, "disabled": 2}


def test_each_observation_of_a_matching_pod_is_counted():
    sender = transport()
    sender.apply_operation(operation(), cases.launch())
    sender.observe_operation(operation())
    sender.observe_operation(operation())
    assert sender.kubernetes_create_reconciliation_total() == {**ZERO, "created": 1, "observed": 2}


def test_a_labelled_pod_without_annotations_is_quarantined_for_its_operation():
    sender = transport()
    pod = existing(sender)
    del pod["metadata"]["annotations"]
    sender.api.objects[NAME] = pod
    assert sender.observe_operation(operation()) == Observation(Phase.REFUSED)
    assert sender.quarantined["uid-1"]["reason"] == "operation"


def test_creation_is_enabled_by_default():
    sender = KubernetesTransport(cases.ApiServer("swarm-pod-proof"), "fixture", PodTemplate(cases.policy()))
    assert sender.creation_enabled is True


def test_a_launch_the_template_refuses_is_refused_without_a_call():
    sender = transport()
    hostile = {**cases.launch(), "provider_account": "cluster-admin-token"}
    assert sender.apply_operation(operation(), hostile) == Observation(Phase.REFUSED)
    assert sender.api.create_calls == 0


@pytest.mark.parametrize(
    "change",
    [
        {"execution_id": "exe-" + "a" * 32},
        {"generation": 4},
    ],
)
def test_a_launch_for_another_execution_or_generation_is_refused(change):
    sender = transport()
    assert sender.apply_operation(operation(), {**cases.launch(), **change}) == Observation(Phase.REFUSED)
    assert sender.api.create_calls == 0


def test_a_swarm_whose_owner_differs_from_the_policy_owner_creates_nothing():
    sender = transport(slug="other")
    assert sender.owner == "agentihooks-swarm-other"
    assert sender.apply_operation(operation(), cases.launch()) == Observation(Phase.REFUSED)
    assert sender.api.create_calls == 0


def test_a_policy_namespace_outside_the_client_namespace_creates_nothing():
    sender = transport(api=cases.ApiServer("default"))
    assert sender.apply_operation(operation(), cases.launch()) == Observation(Phase.REFUSED)
    assert sender.api.create_calls == 0


def test_a_lost_create_response_is_adopted_on_already_exists():
    sender = transport()
    sender.api.drop_next_response = True
    with pytest.raises(TimeoutError):
        sender.apply_operation(operation(), cases.launch())
    assert sender.apply_operation(operation(), cases.launch()) == applied()
    assert len(sender.api.objects) == 1
    assert sender.api.create_calls == 2
    assert sender.kubernetes_create_reconciliation_total() == {**ZERO, "adopted": 1}


def test_a_lost_create_response_is_found_by_observation():
    sender = transport()
    sender.api.drop_next_response = True
    with pytest.raises(TimeoutError):
        sender.apply_operation(operation(), cases.launch())
    assert sender.observe_operation(operation()) == applied()
    assert sender.kubernetes_create_reconciliation_total() == {**ZERO, "observed": 1}


def test_observation_without_a_pod_is_absent():
    sender = transport()
    assert sender.observe_operation(operation()) == Observation(Phase.ABSENT)


class Lists:
    namespace = "swarm-pod-proof"

    def __init__(self):
        self.selectors = []

    def list_pods(self, selector):
        self.selectors.append(selector)
        return []


def test_observation_lists_by_owner_and_execution_labels():
    api = Lists()
    transport(api=api).observe_operation(operation())
    assert api.selectors == [
        f"swarm.agentihooks.io/controller-owner={OWNER},swarm.agentihooks.io/execution-id={EXECUTION}"
    ]


class Refuses:
    namespace = "swarm-pod-proof"

    def __init__(self, on_create=None, on_read=None):
        self.on_create, self.on_read = on_create, on_read

    def list_pods(self, selector):
        raise ApiRefused(403, "Forbidden")

    def create_pod(self, body):
        raise self.on_create

    def read_pod(self, name):
        if self.on_read:
            raise self.on_read
        return None


def test_an_unreadable_list_is_unknown():
    assert transport(api=Refuses()).observe_operation(operation()) == Observation(Phase.UNKNOWN)


def test_a_refused_create_is_refused():
    sender = transport(api=Refuses(on_create=ApiRefused(422, "Invalid")))
    assert sender.apply_operation(operation(), cases.launch()) == Observation(Phase.REFUSED)


def test_already_exists_with_the_pod_gone_is_unknown():
    sender = transport(api=Refuses(on_create=AlreadyExists(NAME)))
    assert sender.apply_operation(operation(), cases.launch()) == Observation(Phase.UNKNOWN)


def test_already_exists_with_an_unreadable_pod_is_unknown():
    sender = transport(api=Refuses(on_create=AlreadyExists(NAME), on_read=ApiRefused(403, "Forbidden")))
    assert sender.apply_operation(operation(), cases.launch()) == Observation(Phase.UNKNOWN)


def existing(sender, **changes) -> dict:
    body = PodTemplate(cases.policy()).render(cases.launch()).pod
    body["metadata"]["annotations"][runtime.OPERATION_DIGEST] = DIGEST
    body["metadata"]["annotations"][runtime.SPEC_DIGEST] = PodTemplate(cases.policy()).render(cases.launch()).digest
    for key, value in changes.items():
        place = "annotations" if key in (runtime.OPERATION_DIGEST, runtime.SPEC_DIGEST) else "labels"
        if value is None:
            del body["metadata"][place][key]
        else:
            body["metadata"][place][key] = value
    return sender.api.put(body)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({watch.OWNER_LABEL: "agentihooks-swarm-other"}, "owner"),
        ({watch.OWNER_LABEL: None}, "owner"),
        ({watch.EXECUTION_LABEL: "exe-" + "e" * 32}, "execution"),
        ({runtime.GENERATION_LABEL: "2"}, "generation"),
        ({runtime.OPERATION_DIGEST: "other"}, "operation"),
        ({runtime.OPERATION_DIGEST: None}, "operation"),
        ({runtime.SPEC_DIGEST: "sha256:other"}, "spec"),
        ({runtime.SPEC_DIGEST: None}, "spec"),
    ],
)
def test_an_already_existing_pod_that_does_not_match_is_quarantined(change, reason):
    sender = transport()
    pod = existing(sender, **change)
    before = json.dumps(sender.api.objects, sort_keys=True)
    assert sender.apply_operation(operation(), cases.launch()) == Observation(Phase.REFUSED)
    assert json.dumps(sender.api.objects, sort_keys=True) == before
    assert sender.quarantined == {"uid-1": {"name": pod["metadata"]["name"], "uid": "uid-1", "reason": reason}}
    assert sender.kubernetes_create_reconciliation_total() == {**ZERO, "quarantined": 1}


def test_an_already_existing_pod_without_labels_or_annotations_is_quarantined_as_foreign():
    sender = transport()
    sender.api.put({"metadata": {"name": NAME, "namespace": "swarm-pod-proof"}})
    assert sender.apply_operation(operation(), cases.launch()) == Observation(Phase.REFUSED)
    assert sender.quarantined == {"uid-1": {"name": NAME, "uid": "uid-1", "reason": "owner"}}


def test_observation_quarantines_a_pod_whose_spec_digest_differs_from_the_journal():
    sender = transport()
    existing(sender, **{runtime.SPEC_DIGEST: "sha256:older-template"})
    assert sender.observe_operation(operation()) == Observation(Phase.REFUSED)
    assert sender.quarantined["uid-1"]["reason"] == "spec"


def test_observation_refuses_a_matching_pod_when_the_journal_holds_no_spec_digest():
    sender = transport()
    existing(sender)
    assert sender.observe_operation(operation(spec_digest="")) == Observation(Phase.REFUSED)
    assert sender.quarantined["uid-1"]["reason"] == "spec"


def test_observation_refuses_a_pod_whose_live_fields_changed_after_creation():
    sender = transport()
    pod = existing(sender)
    pod["spec"]["containers"][0]["image"] = "registry.example/agent@sha256:" + "f" * 64
    sender.api.objects[NAME] = pod
    assert sender.observe_operation(operation()) == Observation(Phase.REFUSED)
    assert sender.quarantined["uid-1"]["reason"] == "spec"


def test_observation_matches_an_admission_defaulted_pod():
    sender = transport()
    sender.api.objects[NAME] = defaulted(existing(sender))
    assert sender.observe_operation(operation()) == applied()


def test_observation_quarantines_a_labelled_pod_without_a_spec():
    sender = transport()
    pod = existing(sender)
    del pod["spec"]
    sender.api.objects[NAME] = pod
    assert sender.observe_operation(operation()) == Observation(Phase.REFUSED)
    assert sender.quarantined["uid-1"]["reason"] == "spec"


class Bare:
    backend = "kubernetes"


def test_a_fenced_transport_passes_the_spec_digest_through_or_answers_empty():
    assert FencedTransport(transport(), lambda: None).spec_digest(cases.launch()) == journaled()
    assert FencedTransport(Bare(), lambda: None).spec_digest(cases.launch()) == ""


def test_spec_digest_is_the_identity_of_the_rendered_pod_and_its_fields():
    sender = transport()
    assert sender.spec_digest(cases.launch()) == journaled()
    assert journaled() != PodTemplate(cases.policy()).render(cases.launch()).digest


def test_a_refused_launch_counts_one_refusal_across_prepare_and_apply():
    sender = transport()
    hostile = {**cases.launch(), "provider_account": "cluster-admin-token"}
    assert sender.spec_digest(hostile) == ""
    assert sender.apply_operation(operation(spec_digest=""), hostile) == Observation(Phase.REFUSED)
    assert sender.template.pod_spec_validation_failures_total() == {"account": 1}
    assert sender.api.create_calls == 0


def test_apply_reuses_the_prepared_render_only_once():
    sender = transport()
    render, renders = sender.template.render, []
    sender.template.render = lambda payload: renders.append(payload) or render(payload)
    sender.spec_digest(cases.launch())
    assert sender.apply_operation(operation(), cases.launch()) == applied()
    assert len(renders) == 1
    sender.apply_operation(operation(), cases.launch())
    assert len(renders) == 2


def test_a_prepared_render_of_another_payload_is_not_reused():
    sender = transport()
    other = {**cases.launch(), "task_payload": {"prompt": "Other."}}
    sender.spec_digest(other)
    assert sender.apply_operation(operation(), cases.launch()) == applied()
    pod = sender.api.objects[NAME]
    assert (
        pod["metadata"]["annotations"][runtime.SPEC_DIGEST] == PodTemplate(cases.policy()).render(cases.launch()).digest
    )


def test_spec_digest_of_a_refused_launch_is_empty():
    hostile = {**cases.launch(), "provider_account": "cluster-admin-token"}
    assert transport().spec_digest(hostile) == ""


def test_apply_refuses_a_launch_whose_render_differs_from_the_journal():
    sender = transport()
    assert sender.apply_operation(operation(spec_digest="sha256:journaled"), cases.launch()) == Observation(
        Phase.REFUSED
    )
    assert sender.api.create_calls == 0


def test_apply_refuses_a_valid_launch_when_the_journal_holds_no_spec_digest():
    sender = transport()
    assert sender.apply_operation(operation(spec_digest=""), cases.launch()) == Observation(Phase.REFUSED)
    assert sender.api.create_calls == 0


def defaulted(pod: dict) -> dict:
    # The fields anton-prod's API server, LimitRange and RuntimeClass admission added in a server dry run.
    live = json.loads(json.dumps(pod))
    spec = live["spec"]
    spec.update(
        {
            "dnsPolicy": "ClusterFirst",
            "schedulerName": "default-scheduler",
            "serviceAccount": spec["serviceAccountName"],
            "priority": 0,
            "preemptionPolicy": "PreemptLowerPriority",
            "overhead": {"cpu": "250m", "memory": "130Mi"},
            "nodeName": "anton-node-1",
        }
    )
    for key in ("hostNetwork", "hostPID", "hostIPC"):
        spec.pop(key)
    spec["nodeSelector"]["katacontainers.io/kata-runtime"] = "true"
    spec["tolerations"].append(
        {"key": "node.kubernetes.io/not-ready", "operator": "Exists", "effect": "NoExecute", "tolerationSeconds": 300}
    )
    for volume in spec["volumes"]:
        size = volume.get("emptyDir", {}).get("sizeLimit", "")
        if size.endswith("Mi") and int(size[:-2]) % 1024 == 0:
            volume["emptyDir"]["sizeLimit"] = f"{int(size[:-2]) // 1024}Gi"
    for container in spec["containers"]:
        container.update({"terminationMessagePath": "/dev/termination-log", "terminationMessagePolicy": "File"})
        container["resources"]["limits"]["cpu"] = "2"
        for mode in ("startup", "readiness", "liveness"):
            container[f"{mode}Probe"]["successThreshold"] = 1
    return live


def test_an_admission_defaulted_pod_is_adopted():
    sender = transport()
    pod = existing(sender)
    sender.api.objects[NAME] = defaulted(pod)
    assert sender.apply_operation(operation(), cases.launch()) == applied()
    assert sender.kubernetes_create_reconciliation_total() == {**ZERO, "adopted": 1}


def tamper_image(spec):
    spec["containers"][0]["image"] = "registry.example/agent@sha256:" + "f" * 64


def tamper_arguments(spec):
    spec["containers"][0]["args"].append("--debug")


def tamper_secret(spec):
    spec["containers"][0]["env"][0]["valueFrom"]["secretKeyRef"]["name"] = "cluster-admin"


def tamper_account(spec):
    spec["serviceAccountName"] = "controller"


def tamper_security(spec):
    spec["containers"][0]["securityContext"]["privileged"] = True


@pytest.mark.parametrize(
    ("tamper", "reason"),
    [
        (tamper_image, "image"),
        (tamper_arguments, "arguments"),
        (tamper_secret, "secrets"),
        (tamper_account, "service-account"),
        (tamper_security, "security-context"),
    ],
)
def test_an_already_existing_pod_whose_live_fields_differ_is_quarantined(tamper, reason):
    sender = transport()
    pod = defaulted(existing(sender))
    tamper(pod["spec"])
    sender.api.objects[NAME] = pod
    assert sender.apply_operation(operation(), cases.launch()) == Observation(Phase.REFUSED)
    assert sender.quarantined == {"uid-1": {"name": NAME, "uid": "uid-1", "reason": reason}}
    assert sender.kubernetes_create_reconciliation_total() == {**ZERO, "quarantined": 1}


def test_a_controller_spawn_journals_the_rendered_spec_digest():
    world = cases.World()
    with world.clocked():
        controller, transport = world.controller()
        assert controller.acquire()
        attempt = world.admit(controller)
        request = world.request(controller, attempt)
        created = controller.execute(request)
        journaled = world.store.operation_journal.get(cases.SLUG, created.operation_id)
    assert created.phase is Phase.APPLIED
    rendered = PodTemplate(cases.policy()).render(request.payload)
    assert journaled.spec_digest == adoption.identity(rendered.digest, rendered.pod["spec"])
    pod = world.api.objects[f"swarm-{attempt.execution_id}"]
    assert pod["metadata"]["annotations"][runtime.SPEC_DIGEST] == rendered.digest


def test_observation_quarantines_a_labelled_pod_of_another_generation():
    sender = transport()
    existing(sender, **{runtime.GENERATION_LABEL: "2"})
    assert sender.observe_operation(operation()) == Observation(Phase.REFUSED)
    assert sender.quarantined["uid-1"]["reason"] == "generation"


def test_two_matching_pods_for_one_execution_are_both_quarantined():
    sender = transport()
    existing(sender)
    twin = PodTemplate(cases.policy()).render(cases.launch()).pod
    twin["metadata"]["name"] = "swarm-twin"
    twin["metadata"]["annotations"][runtime.OPERATION_DIGEST] = DIGEST
    twin["metadata"]["annotations"][runtime.SPEC_DIGEST] = PodTemplate(cases.policy()).render(cases.launch()).digest
    sender.api.put(twin)
    assert sender.observe_operation(operation()) == Observation(Phase.REFUSED)
    assert {uid: q["reason"] for uid, q in sender.quarantined.items()} == {"uid-1": "ambiguous", "uid-2": "ambiguous"}
    assert sender.kubernetes_create_reconciliation_total() == {**ZERO, "quarantined": 2}


def test_two_pods_one_mismatched_keeps_each_reason():
    sender = transport()
    existing(sender)
    twin = PodTemplate(cases.policy()).render(cases.launch()).pod
    twin["metadata"]["name"] = "swarm-twin"
    sender.api.put(twin)
    assert sender.observe_operation(operation()) == Observation(Phase.REFUSED)
    assert {uid: q["reason"] for uid, q in sender.quarantined.items()} == {"uid-1": "ambiguous", "uid-2": "operation"}


def test_pod_status_keeps_phase_and_reasons_apart_from_task_state():
    pod = {
        "metadata": {"name": "p", "uid": "u", "deletionTimestamp": "2026-10-10T00:00:00Z"},
        "status": {
            "phase": "Failed",
            "reason": "Evicted",
            "containerStatuses": [
                {"state": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}},
                {"state": {"waiting": {"reason": "ImagePullBackOff"}}},
                {"state": {"running": {"startedAt": "now"}}},
            ],
        },
    }
    assert pod_status(pod) == PodStatus("p", "u", "Failed", ("Evicted", "OOMKilled", "ImagePullBackOff"), True)


def test_pod_status_without_status_is_unknown_and_live():
    assert pod_status({"metadata": {"name": "p", "uid": "u"}}) == PodStatus("p", "u", "Unknown", (), False)


@pytest.mark.parametrize("phase", ["Pending", "Running", "Succeeded"])
def test_pod_status_reports_each_phase(phase):
    status = pod_status({"metadata": {"name": "p", "uid": "u"}, "status": {"phase": phase, "reason": ""}})
    assert (status.phase, status.reasons) == (phase, ())


def test_status_reads_the_labelled_pod_of_one_execution():
    sender = transport()
    assert sender.status(EXECUTION) == ()
    sender.apply_operation(operation(), cases.launch())
    assert sender.status(EXECUTION) == (PodStatus(NAME, "uid-1", "Pending", (), False),)


def test_status_of_an_ambiguous_execution_lists_every_pod():
    sender = transport()
    existing(sender)
    twin = PodTemplate(cases.policy()).render(cases.launch()).pod
    twin["metadata"]["name"] = "swarm-twin"
    sender.api.put(twin)
    assert [status.uid for status in sender.status(EXECUTION)] == ["uid-1", "uid-2"]


def test_an_unanswered_observation_is_retried_with_one_create():
    world = cases.World()
    calls = {"lists": 0}
    original = world.api.list_pods

    def flaky(selector):
        calls["lists"] += 1
        if calls["lists"] == 1:
            raise ConnectionError("list reset")
        return original(selector)

    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        attempt = world.admit(controller)
        world.api.list_pods = flaky
        request = world.request(controller, attempt)
        first = controller.execute(request)
        retried = controller.execute(request)
    assert (first.phase, retried.phase) == (Phase.UNKNOWN, Phase.APPLIED)
    assert world.api.create_calls == 1


def test_a_sent_create_with_no_pod_left_is_unknown_and_an_unsent_one_absent():
    sender = transport()
    sent = Operation("op-1", EXECUTION, 3, "spawn", "kubernetes", DIGEST, {}, Phase.UNKNOWN, attempted=True)
    assert sender.observe_operation(sent) == Observation(Phase.UNKNOWN)
    assert sender.observe_operation(operation()) == Observation(Phase.ABSENT)


def lost(world):
    controller, _ = world.controller()
    assert controller.acquire()
    attempt = world.admit(controller)
    request = world.request(controller, attempt)
    world.api.drop_next_response = True
    first = controller.execute(request)
    return controller, request, first


def test_a_pod_reaped_after_a_lost_response_is_never_created_again():
    world = cases.World()
    with world.clocked():
        controller, request, first = lost(world)
        assert world.store.operation_journal.get(cases.SLUG, first.operation_id).attempted is True
        world.api.objects.clear()
        retried = controller.execute(request)
    assert (first.phase, retried.phase) == (Phase.UNKNOWN, Phase.UNKNOWN)
    assert world.api.create_calls == 1


def test_a_held_create_is_not_marked_sent_and_runs_once_creation_returns():
    world = cases.World()
    with world.clocked():
        held, _ = world.controller(creation_enabled=False)
        assert held.acquire()
        attempt = world.admit(held)
        first = held.execute(world.request(held, attempt))
        world.clock[0] += cases.lease.ttl_ms()
        enabled, _ = world.controller()
        assert enabled.acquire()
        created = enabled.execute(world.request(enabled, attempt))
    assert (first.phase, first.attempted) == (Phase.ACCEPTED, False)
    assert (created.phase, world.api.create_calls) == (Phase.APPLIED, 1)


def test_a_refused_create_clears_the_sent_mark():
    world = cases.World()
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        attempt = world.admit(controller)
        world.api.put(
            {"metadata": {"name": f"swarm-{attempt.execution_id}", "namespace": world.api.namespace, "labels": {}}}
        )
        refused = controller.execute(world.request(controller, attempt))
    assert (refused.phase, refused.attempted) == (Phase.REFUSED, False)


def test_a_sent_mark_survives_the_prepare_of_a_retry():
    world = cases.World()
    with world.clocked():
        controller, request, first = lost(world)
        again = world.store.operation_journal.prepare(cases.SLUG, request)
    assert (again.operation_id, again.attempted, again.phase) == (first.operation_id, True, Phase.UNKNOWN)


def test_a_created_pod_is_matched_by_the_controller_reconcile():
    world = cases.World()
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        attempt = world.admit(controller)
        created = controller.execute(world.request(controller, attempt))
        plan = controller.reconcile()
    assert plan.matched == {attempt.execution_id: created.result["uid"]}
    assert controller.controller_orphans_by_class()["foreign"] == 0


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    first, second = cases.run_case(case), cases.run_case(case)
    assert first == second
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True)
    path = EVIDENCE / f"{case}-result.json"
    committed = json.loads(path.read_text()) if path.exists() else None
    assert committed == first, json.dumps(first, indent=2, sort_keys=True)
