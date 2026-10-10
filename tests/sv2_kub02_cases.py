import copy
import hashlib
import json
from pathlib import Path
from unittest import mock

from scripts.swarm import lease
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm_v2.controller import Controller
from scripts.swarm_v2.kubernetes import watch
from scripts.swarm_v2.kubernetes.client import AlreadyExists
from scripts.swarm_v2.kubernetes.runtime import KubernetesTransport
from scripts.swarm_v2.kubernetes.spec import PodTemplate, load_policy
from scripts.swarm_v2.runtime.operations import OperationRequest

FIXTURES = Path(__file__).parent / "fixtures" / "swarm_v2"
INPUTS = ("pod-policy.json", "pod-launch.json")
SLUG = "fixture"
EVIDENCE_CLASS = (
    "mocked: fakeredis journal and execution registry, a fake lease clock and an in process API server that applies "
    "a create, drops its response and then answers AlreadyExists; no Kubernetes API server was called"
)


def policy() -> dict:
    return load_policy(FIXTURES / "pod-policy.json")


def launch() -> dict:
    return json.loads((FIXTURES / "pod-launch.json").read_text())


class ApiServer:
    """Deterministic UIDs, label selector lists and an optional dropped create response."""

    def __init__(self, namespace: str) -> None:
        self.namespace = namespace
        self.objects: dict[str, dict] = {}
        self.create_calls = 0
        self.drop_next_response = False

    def put(self, pod: dict) -> dict:
        pod = copy.deepcopy(pod)
        pod["metadata"]["uid"] = f"uid-{len(self.objects) + 1}"
        pod["status"] = {"phase": "Pending"}
        self.objects[pod["metadata"]["name"]] = pod
        return copy.deepcopy(pod)

    def create_pod(self, body: dict) -> dict:
        self.create_calls += 1
        if body["metadata"]["name"] in self.objects:
            raise AlreadyExists(body["metadata"]["name"])
        created = self.put(body)
        if self.drop_next_response:
            self.drop_next_response = False
            raise TimeoutError("create response dropped")
        return created

    def read_pod(self, name: str) -> dict | None:
        return copy.deepcopy(self.objects.get(name))

    def list_pods(self, selector: str) -> list[dict]:
        terms = [term.partition("=") for term in selector.split(",")]
        return [
            copy.deepcopy(pod)
            for pod in self.objects.values()
            if all(
                key in pod["metadata"].get("labels", {}) and (not equals or pod["metadata"]["labels"][key] == value)
                for key, equals, value in terms
            )
        ]


class Source:
    """The watch PodSource over the same API server, for PodView and Controller.reconcile."""

    def __init__(self, api: ApiServer) -> None:
        self.api = api

    def _pod(self, raw: dict) -> watch.Pod:
        metadata = raw["metadata"]
        return watch.Pod(metadata["name"], metadata["uid"], metadata.get("labels", {}), "deletionTimestamp" in metadata)

    def list_pods(self, selector):
        return [self._pod(raw) for raw in self.api.list_pods(selector)], "1"

    def watch_pods(self, selector, resource_version):
        raise watch.CursorExpired()

    def read_pod(self, name):
        raw = self.api.read_pod(name)
        return self._pod(raw) if raw else None

    def delete_pod(self, name, uid):
        if self.api.objects[name]["metadata"]["uid"] == uid:
            self.api.objects.pop(name)


class World:
    def __init__(self) -> None:
        import fakeredis

        self.redis = fakeredis.FakeServer()
        self.clock = [1000]
        self.api = ApiServer(policy()["namespace"])
        self.store = self.client()
        self.store.create(SwarmConfig(SLUG, "agentihooks", 1, 0))
        self.grant = {"allowed": True}

    def client(self) -> RedisStore:
        import fakeredis

        return RedisStore(fakeredis.FakeRedis(server=self.redis, decode_responses=True))

    def clocked(self):
        return mock.patch.object(lease, "now_ms", lambda store: self.clock[0])

    def controller(self, creation_enabled: bool = True) -> tuple[Controller, KubernetesTransport]:
        transport = KubernetesTransport(self.api, SLUG, PodTemplate(policy()), creation_enabled)
        view = watch.PodView(Source(self.api))
        return Controller(self.client(), SLUG, [transport], lambda: self.grant["allowed"], pods=view), transport

    def admit(self, controller: Controller, seat: str = "eng-1") -> AgentRecord:
        target = {"pod_namespace": self.api.namespace, "pod_name": "swarm-pending"}
        record = AgentRecord(
            self.store.next_name(SLUG, "eng"),
            "eng",
            "task",
            seat=f"{seat}@{SLUG}",
            runtime_backend="kubernetes",
            runtime_target=target,
        )
        return controller.admit(record)

    def request(self, controller: Controller, attempt: AgentRecord, base: dict | None = None) -> OperationRequest:
        payload = {
            **(base or launch()),
            "execution_id": attempt.execution_id,
            "generation": attempt.generation,
            "seat_id": attempt.seat,
            "controller_epoch": next(
                intent["controller_epoch"]
                for intent in controller.intents()
                if intent["execution_id"] == attempt.execution_id
            ),
        }
        return OperationRequest(attempt.execution_id, attempt.generation, "spawn", payload, "create")


def second_launch() -> dict:
    return {**launch(), "harness": "codex", "credential_ref": "swarm-codex-fixture", "task_payload": {"prompt": "Two."}}


def _lost_response(base: dict) -> dict:
    world = World()
    with world.clocked():
        controller, transport = world.controller()
        assert controller.acquire()
        attempt = world.admit(controller)
        request = world.request(controller, attempt, base)
        world.api.drop_next_response = True
        interrupted = controller.execute(request)
        retried = controller.execute(request)
        journal_creates = world.api.create_calls
        operation = world.store.operation_journal.get(SLUG, retried.operation_id)
        direct = transport.apply_operation(operation, request.payload)
        pod = world.api.read_pod(f"swarm-{attempt.execution_id}")
        return {
            "first_attempt_phase": interrupted.phase.value,
            "retry_phase": retried.phase.value,
            "journal_phase": operation.phase.value,
            "journal_uid": operation.result.get("uid"),
            "pod_uid": pod["metadata"]["uid"],
            "pods_in_namespace": len(world.api.objects),
            "create_calls_through_the_journal": journal_creates,
            "create_calls": world.api.create_calls,
            "direct_retry_phase": direct.phase.value,
            "direct_retry_uid": direct.result.get("uid"),
            "pod_labels_select_it": [
                p["metadata"]["uid"]
                for p in world.api.list_pods(
                    f"{watch.OWNER_LABEL}={watch.owner_for(SLUG)},{watch.EXECUTION_LABEL}={attempt.execution_id}"
                )
            ],
            "kubernetes_create_reconciliation_total": transport.kubernetes_create_reconciliation_total(),
        }


def _positive() -> tuple[dict, bool]:
    first, second = _lost_response(launch()), _lost_response(second_launch())
    checks = [
        run["first_attempt_phase"] == "unknown"
        and run["retry_phase"] == run["journal_phase"] == run["direct_retry_phase"] == "applied"
        and run["journal_uid"] == run["pod_uid"] == run["direct_retry_uid"] == "uid-1"
        and run["pods_in_namespace"] == 1
        and run["create_calls_through_the_journal"] == 1
        and run["create_calls"] == 2
        and run["pod_labels_select_it"] == ["uid-1"]
        and run["kubernetes_create_reconciliation_total"]
        == {"created": 0, "adopted": 1, "observed": 1, "quarantined": 0, "disabled": 0}
        for run in (first, second)
    ]
    second_input = hashlib.sha256(json.dumps(second_launch(), sort_keys=True).encode()).hexdigest()
    return {"fixture": first, "second_fixture": second, "second_fixture_input_sha256": second_input}, all(checks)


def _rejection() -> tuple[dict, bool]:
    world = World()
    with world.clocked():
        controller, transport = world.controller()
        assert controller.acquire()
        attempt = world.admit(controller)
        request = world.request(controller, attempt)
        squatter = {
            "metadata": {
                "name": f"swarm-{attempt.execution_id}",
                "namespace": world.api.namespace,
                "labels": watch.labels(watch.owner_for(SLUG), "exe-" + "e" * 32),
                "annotations": {"swarm.agentihooks.io/seat": "eng-9@fixture"},
            }
        }
        world.api.put(squatter)
        protected = copy.deepcopy(world.api.objects)
        refused = controller.execute(request)
        repeated = controller.execute(request)
        creates = world.api.create_calls
        world.grant["allowed"] = False
        try:
            controller.execute(request)
            revoked = "executed"
        except SwarmError as error:
            revoked = str(error)
        observed = {
            "phase": refused.phase.value,
            "repeated_phase": repeated.phase.value,
            "revoked_grant": revoked,
            "create_calls_after_revoke": world.api.create_calls - creates,
            "result": refused.result,
            "quarantined": sorted(transport.quarantined.values(), key=lambda q: q["uid"]),
            "api_objects_unchanged": world.api.objects == protected,
            "pods_in_namespace": len(world.api.objects),
            "kubernetes_create_reconciliation_total": transport.kubernetes_create_reconciliation_total(),
        }
    checks = [
        observed["phase"] == observed["repeated_phase"] == "refused",
        observed["revoked_grant"] == "a scoped controller grant is required",
        observed["create_calls_after_revoke"] == 0,
        observed["result"] == {},
        observed["quarantined"] == [{"name": f"swarm-{attempt.execution_id}", "uid": "uid-1", "reason": "execution"}],
        observed["api_objects_unchanged"],
        observed["kubernetes_create_reconciliation_total"]
        == {"created": 0, "adopted": 0, "observed": 0, "quarantined": 2, "disabled": 0},
    ]
    observed["quarantined"] = [{**q, "name": "swarm-<execution>"} for q in observed["quarantined"]]
    return observed, all(checks)


def _recovery() -> tuple[dict, bool]:
    world = World()
    with world.clocked():
        crashed, _ = world.controller()
        assert crashed.acquire()
        attempt = world.admit(crashed)
        request = world.request(crashed, attempt)
        world.api.drop_next_response = True
        interrupted = crashed.execute(request)
        world.clock[0] += lease.ttl_ms()
        restarted, transport = world.controller()
        assert restarted.acquire()
        recovered = world.store.operation_journal.get(SLUG, interrupted.operation_id)
        plan = restarted.reconcile()
        replay = restarted.execute(world.request(restarted, attempt))
        try:
            crashed.execute(request)
            stale = "executed"
        except SwarmError as error:
            stale = str(error)
        pod = world.api.objects[f"swarm-{attempt.execution_id}"]
        pod["status"] = {
            "phase": "Succeeded",
            "containerStatuses": [{"state": {"terminated": {"reason": "Completed", "exitCode": 0}}}],
        }
        (status,) = transport.status(attempt.execution_id)
        execution = world.store.execution(SLUG, attempt.execution_id)
        world.clock[0] += lease.ttl_ms()
        rollback, disabled = world.controller(creation_enabled=False)
        assert rollback.acquire()
        later = world.admit(rollback, seat="eng-2")
        held = rollback.execute(world.request(rollback, later))
        observed = {
            "interrupted_phase": interrupted.phase.value,
            "recovered_phase": recovered.phase.value,
            "recovered_uid": recovered.result.get("uid"),
            "reconcile_matched_uid": plan.matched.get(attempt.execution_id),
            "replay_phase": replay.phase.value,
            "create_calls": world.api.create_calls,
            "stale_controller": stale,
            "pod_status": {"phase": status.phase, "reasons": list(status.reasons), "deleting": status.deleting},
            "execution_record_unchanged": execution == attempt,
            "rollback": {
                "new_launch_phase": held.phase.value,
                "create_calls_after_rollback": world.api.create_calls,
                "existing_pod_still_observed": [found.uid for found in disabled.status(attempt.execution_id)],
                "kubernetes_create_reconciliation_total": disabled.kubernetes_create_reconciliation_total(),
            },
            "kubernetes_create_reconciliation_total": transport.kubernetes_create_reconciliation_total(),
        }
    checks = [
        observed["interrupted_phase"] == "unknown",
        observed["recovered_phase"] == observed["replay_phase"] == "applied",
        observed["recovered_uid"] == observed["reconcile_matched_uid"] == "uid-1",
        observed["create_calls"] == 1,
        observed["stale_controller"] == "the controller lease is stale",
        observed["pod_status"] == {"phase": "Succeeded", "reasons": ["Completed"], "deleting": False},
        observed["execution_record_unchanged"],
        observed["rollback"]["new_launch_phase"] == "accepted",
        observed["rollback"]["create_calls_after_rollback"] == 1,
        observed["rollback"]["existing_pod_still_observed"] == ["uid-1"],
        observed["rollback"]["kubernetes_create_reconciliation_total"]["disabled"] == 1,
    ]
    return observed, all(checks)


def run_case(case: str) -> dict:
    observed, passed = {"a": _positive, "b": _rejection, "c": _recovery}[case]()
    return {
        "case": f"T-SV2-KUB-02-{case.upper()}",
        "state": "passed" if passed else "failed",
        "evidence_class": EVIDENCE_CLASS,
        "input_sha256": {name: hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() for name in INPUTS},
        "observed": observed,
    }
