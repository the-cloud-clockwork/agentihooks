import json
from dataclasses import asdict

import pytest

from scripts.swarm import lease
from scripts.swarm_v2.kubernetes import runtime, watch
from scripts.swarm_v2.kubernetes.adapter import KubernetesRuntime
from scripts.swarm_v2.kubernetes.client import ApiRefused
from scripts.swarm_v2.runtime.base import Outcome, SpawnRequest, Status
from scripts.swarm_v2.runtime.operations import Observation, Operation, Operations, Phase
from tests import sv2_kub02_cases as cases
from tests.test_swarm_v2_kubernetes_runtime import defaulted, existing, operation, tamper_security, transport

pytestmark = pytest.mark.unit


def squat(world, attempt) -> None:
    world.api.put(
        {
            "metadata": {
                "name": f"swarm-{attempt.execution_id}",
                "namespace": world.api.namespace,
                "labels": watch.labels(watch.owner_for(cases.SLUG), "exe-" + "e" * 32),
            }
        }
    )


def restart(world):
    world.clock[0] += lease.ttl_ms()
    restarted, _ = world.controller()
    assert restarted.acquire()
    return restarted


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({watch.OWNER_LABEL: "agentihooks-swarm-other"}, "owner"),
        ({runtime.GENERATION_LABEL: "2"}, "generation"),
        ({runtime.OPERATION_DIGEST: "other"}, "operation"),
        ({runtime.SPEC_DIGEST: "sha256:other"}, "spec"),
    ],
)
def test_a_mismatched_pod_refusal_names_the_field(change, field):
    sender = transport()
    existing(sender, **change)
    observed = sender.apply_operation(operation(), cases.launch())
    assert observed.phase is Phase.REFUSED
    assert observed.refusal == {"reason": "mismatch", "field": field}


def test_a_live_pod_whose_security_context_drifted_names_the_field_group():
    sender = transport()
    pod = defaulted(existing(sender))
    tamper_security(pod["spec"])
    sender.api.objects[pod["metadata"]["name"]] = pod
    observed = sender.apply_operation(operation(), cases.launch())
    assert observed.refusal == {"reason": "mismatch", "field": "security-context"}


def test_an_identity_mismatch_of_an_applied_observation_is_named():
    class Lying:
        backend = "kubernetes"

        def observe_operation(self, operation):
            return Observation(Phase.APPLIED, "exe-other", 1, "kubernetes", "d")

    world = cases.World()
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        attempt = world.admit(controller)
        request = world.request(controller, attempt)
        refused = Operations(world.store, [Lying()]).execute(cases.SLUG, request)
    assert refused.phase is Phase.REFUSED
    assert refused.refusal == {"reason": "identity", "field": ""}


def test_a_journaled_refusal_survives_an_unknown_observation():
    world = cases.World()
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        attempt = world.admit(controller)
        squat(world, attempt)
        refused = controller.execute(world.request(controller, attempt))
        unknown = world.store.operation_journal.settle(cases.SLUG, refused, Observation(Phase.UNKNOWN))
    assert unknown.phase is Phase.UNKNOWN
    assert unknown.refusal == {"reason": "mismatch", "field": "execution"}


def test_two_pods_for_one_execution_refuse_as_ambiguous():
    sender = transport()
    existing(sender)
    twin = json.loads(json.dumps(sender.api.objects[f"swarm-{operation().execution_id}"]))
    twin["metadata"]["name"] = "swarm-twin"
    sender.api.put(twin)
    assert sender.observe_operation(operation()).refusal == {"reason": "ambiguous", "field": ""}


def test_a_launch_the_template_refuses_names_the_template_field():
    observed = transport().apply_operation(operation(), {**cases.launch(), "harness": "bash"})
    assert observed.refusal == {"reason": "template", "field": "harness"}


@pytest.mark.parametrize(
    ("journal", "field"),
    [({"generation": 4}, "generation"), ({"execution_id": "exe-" + "e" * 32}, "execution"), ({"spec": ""}, "spec")],
)
def test_a_render_that_differs_from_the_journal_names_the_field(journal, field):
    spec = journal.pop("spec", None)
    observed = transport().apply_operation(operation(spec_digest=spec, **journal), cases.launch())
    assert observed.refusal == {"reason": "render", "field": field}


def test_a_render_for_another_swarms_owner_names_the_owner():
    sender = transport(slug="other")
    assert sender.apply_operation(operation(), cases.launch()).refusal == {"reason": "render", "field": "owner"}


def test_a_render_outside_the_client_namespace_names_the_namespace():
    sender = transport(cases.ApiServer("another-namespace"))
    assert sender.apply_operation(operation(), cases.launch()).refusal == {"reason": "render", "field": "namespace"}


class RefusingCreate(cases.ApiServer):
    def create_pod(self, body):
        raise ApiRefused(403, "Forbidden")


def test_an_api_refusal_of_the_create_is_named():
    sender = transport(RefusingCreate(cases.policy()["namespace"]))
    assert sender.apply_operation(operation(), cases.launch()).refusal == {"reason": "api", "field": ""}


def test_a_refused_spawn_is_journaled_and_read_back_by_a_restarted_controller():
    world = cases.World()
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        attempt = world.admit(controller)
        squat(world, attempt)
        refused = controller.execute(world.request(controller, attempt))
        journaled = world.store.operation_journal.get(cases.SLUG, refused.operation_id)
        restarted = restart(world)
        refusals = restarted.spawn_refusals()
    assert refused.phase is Phase.REFUSED
    assert journaled.refusal == {"reason": "mismatch", "field": "execution"}
    assert refusals == {
        attempt.execution_id: {"operation_id": refused.operation_id, "reason": "mismatch", "field": "execution"}
    }


def test_a_template_refusal_survives_the_absent_observation_of_restart_recovery():
    world = cases.World()
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        attempt = world.admit(controller)
        refused = controller.execute(world.request(controller, attempt, {**cases.launch(), "harness": "bash"}))
        restarted = restart(world)
        recovered = world.store.operation_journal.get(cases.SLUG, refused.operation_id)
        refusals = restarted.spawn_refusals()
    assert refused.refusal == {"reason": "template", "field": "harness"}
    assert recovered.phase is Phase.ACCEPTED
    assert recovered.refusal == refused.refusal
    assert refusals[attempt.execution_id]["field"] == "harness"


def test_a_controller_refused_spawn_names_its_reason_and_field_in_the_runtime_status():
    world = cases.World()
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        attempt = world.admit(controller)
        payload = world.request(controller, attempt).payload
        squat(world, attempt)
        task = {"id": "task", "execution_id": attempt.execution_id, "generation": attempt.generation}
        outcome = KubernetesRuntime(controller.execute, lambda _: payload).spawn(
            SpawnRequest(object(), "eng", attempt.name, task)
        )
    assert outcome.status is Status.REFUSED
    assert outcome.detail.endswith(" is refused: mismatch on execution")


def test_an_applied_spawn_clears_its_journaled_refusal():
    world = cases.World()
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        attempt = world.admit(controller)
        request = world.request(controller, attempt)
        squat(world, attempt)
        controller.execute(request)
        world.api.objects.clear()
        applied = controller.execute(request)
        refusals = controller.spawn_refusals()
    assert applied.phase is Phase.APPLIED
    assert applied.refusal == {}
    assert refusals == {}


def test_a_journal_entry_written_before_refusals_decodes_without_one():
    world = cases.World()
    record = asdict(Operation("op-old", "exe-1", 1, "spawn", "kubernetes", "d", {}, Phase.REFUSED))
    record.pop("refusal")
    world.store.redis.hset(world.store.key(cases.SLUG, "runtime-operations"), "op-old", json.dumps(record))
    assert world.store.operation_journal.get(cases.SLUG, "op-old").refusal == {}


class Refusing:
    def __init__(self, refusal):
        self.refusal = refusal

    def execute(self, request):
        return Operation("op-1", "exe-1", 2, "spawn", "kubernetes", "d", {}, Phase.REFUSED, refusal=self.refusal)


def spawn(refusal) -> Outcome:
    request = SpawnRequest(object(), "eng", "e1", {"id": "t1", "execution_id": "exe-1", "generation": 2})
    return KubernetesRuntime(Refusing(refusal).execute, lambda _: {"harness": "codex"}).spawn(request)


def test_the_refused_spawn_status_names_the_reason_and_field():
    outcome = spawn({"reason": "mismatch", "field": "execution"})
    assert outcome == Outcome(
        "spawn", Status.REFUSED, "kubernetes", None, "spawn operation op-1 is refused: mismatch on execution"
    )


def test_a_refusal_without_a_field_names_only_its_reason():
    assert spawn({"reason": "api", "field": ""}).detail == "spawn operation op-1 is refused: api"


def test_the_refusal_is_diagnostic_and_leaves_observation_equality_alone():
    assert Observation(Phase.REFUSED, refusal={"reason": "api", "field": ""}) == Observation(Phase.REFUSED)
