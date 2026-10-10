import copy
import hashlib
import json
from pathlib import Path

from scripts.swarm.store import AgentRecord, SwarmError
from scripts.swarm_v2 import retention
from scripts.swarm_v2.kubernetes import watch
from scripts.swarm_v2.kubernetes.cleanup import Cleanup
from scripts.swarm_v2.kubernetes.client import PreconditionFailed
from scripts.swarm_v2.kubernetes.runtime import GENERATION_LABEL
from tests import sv2_kub02_cases as kub02

FIXTURES = Path(__file__).parent / "fixtures" / "swarm_v2"
INPUTS = ("cleanup-attempts.json", "pod-policy.json", "pod-launch.json")
STORED = ("attempt-finals", "attempt-retained", "cleanup-journal", "executions", "heartbeats")
SLUG = kub02.SLUG
EVIDENCE_CLASS = (
    "mocked: fakeredis execution registry, heartbeat archive watermarks, attempt records and cleanup journal, a fake "
    "lease clock, an in process API server that enforces delete uid preconditions and can drop a delete response, "
    "and in process attach and secret registries; no Kubernetes API server was called"
)


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / "cleanup-attempts.json").read_text())[name]


class ApiServer(kub02.ApiServer):
    """Pods and Services with monotonic UIDs, uid preconditioned deletes and an optional dropped delete response."""

    def __init__(self, namespace: str) -> None:
        super().__init__(namespace)
        self.services: dict[str, dict] = {}
        self.issued = 0
        self.deletes: list[tuple[str, str, str]] = []
        self.drop_next_delete = False

    def put(self, pod: dict) -> dict:
        return self._put(self.objects, pod, {"phase": "Pending"})

    def put_service(self, service: dict) -> dict:
        return self._put(self.services, service, {})

    def _put(self, table: dict, body: dict, status: dict) -> dict:
        body = copy.deepcopy(body)
        self.issued += 1
        body["metadata"]["uid"] = f"uid-{self.issued}"
        body["status"] = status
        table[body["metadata"]["name"]] = body
        return copy.deepcopy(body)

    def list_services(self, selector: str) -> list[dict]:
        objects, self.objects = self.objects, self.services
        try:
            return self.list_pods(selector)
        finally:
            self.objects = objects

    def read(self, kind: str, name: str) -> dict | None:
        return copy.deepcopy((self.objects if kind == "pods" else self.services).get(name))

    def delete(self, kind: str, name: str, uid: str) -> bool:
        self.deletes.append((kind, name, uid))
        table = self.objects if kind == "pods" else self.services
        live = table.get(name)
        if live is None:
            return False
        if live["metadata"]["uid"] != uid:
            raise PreconditionFailed(name)
        table.pop(name)
        if self.drop_next_delete:
            self.drop_next_delete = False
            raise TimeoutError("delete response dropped")
        return True


class Registry:
    """Attach registrations or attempt scoped secret references, released idempotently."""

    def __init__(self, name: str, log: list, held: set) -> None:
        self.name, self.log, self.held = name, log, held
        self.fail_next = False

    def release(self, execution_id: str) -> None:
        self.log.append(self.name)
        if self.fail_next:
            self.fail_next = False
            raise ConnectionError(f"{self.name} registry unavailable")
        self.held.discard(execution_id)


class World(kub02.World):
    def __init__(self) -> None:
        super().__init__()
        self.api = ApiServer(kub02.policy()["namespace"])
        self.log: list[str] = []
        self.attached: set[str] = set()
        self.secret_refs: set[str] = set()
        self.attach = Registry("attach", self.log, self.attached)
        self.secrets = Registry("secrets", self.log, self.secret_refs)

    def cleanup(self, controller, enabled: bool = True) -> Cleanup:
        return Cleanup(self.client(), SLUG, self.api, controller.require, self.attach, self.secrets, enabled)

    def acknowledge(self, execution_id: str, watermark: int) -> None:
        self.store.redis.hset(
            self.store.key(SLUG, "heartbeats"), execution_id, json.dumps({"archive_watermark": watermark})
        )

    def spawn(self, controller, attempt: AgentRecord, fx: dict) -> None:
        controller.execute(self.request(controller, attempt))
        pod = self.api.objects[f"swarm-{attempt.execution_id}"]
        pod["metadata"]["labels"].update(fx["related_labels"])
        self.api.put_service(
            {
                "metadata": {
                    "name": f"swarm-attach-{attempt.execution_id}",
                    "namespace": self.api.namespace,
                    "labels": {
                        **watch.labels(watch.owner_for(SLUG), attempt.execution_id),
                        GENERATION_LABEL: str(attempt.generation),
                        **fx["related_labels"],
                    },
                },
                "spec": {"ports": [{"port": fx["service_port"]}]},
            }
        )
        self.attached.add(attempt.execution_id)
        self.secret_refs.add(attempt.execution_id)

    def attempts(self, controller, fx: dict) -> tuple[AgentRecord, AgentRecord]:
        old = self.admit(controller)
        self.spawn(controller, old, fx)
        target = {"pod_namespace": self.api.namespace, "pod_name": "swarm-pending"}
        record = AgentRecord(
            self.store.next_name(SLUG, "eng"),
            "eng",
            "task",
            seat=old.seat,
            runtime_backend="kubernetes",
            runtime_target=target,
        )
        replacement = controller.admit(record, old.execution_id)
        self.spawn(controller, replacement, fx)
        return old, replacement

    def runtime(self) -> dict:
        return {
            "pods": copy.deepcopy(self.api.objects),
            "services": copy.deepcopy(self.api.services),
            "attached": sorted(self.attached),
            "secret_refs": sorted(self.secret_refs),
        }

    def snapshot(self) -> dict:
        stored = {name: self.store.redis.hgetall(self.store.key(SLUG, name)) for name in STORED}
        return {**self.runtime(), **stored}


def _masked(observed: dict, *attempts: AgentRecord) -> dict:
    text = json.dumps(observed, sort_keys=True)
    for label, attempt in zip(("<old>", "<replacement>"), attempts):
        text = text.replace(attempt.execution_id, label)
    return json.loads(text)


def _names(table: dict) -> list[str]:
    return sorted(table)


def _scoped(world: World, before: dict, old: AgentRecord) -> bool:
    expected = {
        watch.OWNER_LABEL: watch.owner_for(SLUG),
        watch.EXECUTION_LABEL: old.execution_id,
        GENERATION_LABEL: str(old.generation),
    }
    deleted = [before[kind][name]["metadata"] for kind, name, _ in world.api.deletes]
    return bool(deleted) and all(
        meta["namespace"] == kub02.policy()["namespace"]
        and {key: meta["labels"].get(key) for key in expected} == expected
        for meta in deleted
    )


def _clean(name: str) -> dict:
    fx = fixture(name)
    world = World()
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        old, replacement = world.attempts(controller, fx)
        replacement_record = world.store.execution(SLUG, replacement.execution_id)
        cleanup = world.cleanup(controller)
        before = world.runtime()
        not_final = cleanup.run(old.execution_id).state
        retention.finalize(world.store, SLUG, controller.require, old.execution_id, fx["state"], fx["transcript_end"])
        world.acknowledge(old.execution_id, fx["pending_archive_watermark"])
        waiting_outcome = cleanup.run(old.execution_id).state
        retention.finalize(
            world.store,
            SLUG,
            controller.require,
            old.execution_id,
            fx["state"],
            fx["transcript_end"],
            fx["outcome_ref"],
        )
        waiting_archive = cleanup.run(old.execution_id).state
        backlog_pending = retention.execution_cleanup_backlog(world.store, SLUG)
        untouched_while_waiting = world.runtime() == before and world.api.deletes == []
        world.acknowledge(old.execution_id, fx["transcript_end"])
        backlog_ready = retention.execution_cleanup_backlog(world.store, SLUG)
        done = cleanup.run(old.execution_id)
        repeated = cleanup.run(old.execution_id)
        kept = retention.retained(world.store, SLUG, old.execution_id)
        observed = {
            "states": [not_final, waiting_outcome, waiting_archive, done.state, repeated.state],
            "untouched_while_waiting": untouched_while_waiting,
            "execution_cleanup_backlog": {
                "archive_pending": backlog_pending,
                "archive_acknowledged": backlog_ready,
                "after_cleanup": retention.execution_cleanup_backlog(world.store, SLUG),
            },
            "removed": done.removed,
            "deletes": world.api.deletes,
            "deleted_objects_scoped": _scoped(world, before, old),
            "pods_left": _names(world.api.objects),
            "services_left": _names(world.api.services),
            "replacement_pod_unchanged": world.api.objects[f"swarm-{replacement.execution_id}"]
            == before["pods"][f"swarm-{replacement.execution_id}"],
            "releases": world.log,
            "attached": sorted(world.attached),
            "secret_refs": sorted(world.secret_refs),
            "retained": {
                "final": kept["final"],
                "archive_watermark": kept["archive_watermark"],
                "execution_seat": kept["execution"]["seat"],
                "execution_generation": kept["execution"]["generation"],
                "removed_matches": kept["removed"] == done.removed,
            },
            "execution_records_kept": world.store.execution(SLUG, old.execution_id) == old
            and world.store.execution(SLUG, replacement.execution_id) == replacement_record,
            "journal_cleared": world.store.redis.hlen(world.store.key(SLUG, "cleanup-journal")) == 0,
        }
    return _masked(observed, old, replacement)


def _positive() -> tuple[dict, bool]:
    first, second = _clean("first"), _clean("second")
    checks = []
    for run, name in ((first, "first"), (second, "second")):
        fx = fixture(name)
        checks += [
            run["states"] == ["not_final", "waiting_outcome", "waiting_archive", "done", "done"],
            run["untouched_while_waiting"],
            run["execution_cleanup_backlog"]["archive_pending"]
            == {"waiting_outcome": 0, "waiting_archive": 1, "ready": 0},
            run["execution_cleanup_backlog"]["archive_acknowledged"]
            == {"waiting_outcome": 0, "waiting_archive": 0, "ready": 1},
            run["execution_cleanup_backlog"]["after_cleanup"]
            == {"waiting_outcome": 0, "waiting_archive": 0, "ready": 0},
            run["deletes"] == [["pods", "swarm-<old>", "uid-1"], ["services", "swarm-attach-<old>", "uid-2"]],
            run["deleted_objects_scoped"],
            run["pods_left"] == ["swarm-<replacement>"],
            run["services_left"] == ["swarm-attach-<replacement>"],
            run["replacement_pod_unchanged"],
            run["releases"] == ["attach", "secrets"],
            run["attached"] == run["secret_refs"] == ["<replacement>"],
            run["retained"]["final"]
            == {
                "execution_id": "<old>",
                "generation": 1,
                "state": fx["state"],
                "transcript_end": fx["transcript_end"],
                "outcome_ref": fx["outcome_ref"],
            },
            run["retained"]["archive_watermark"] == fx["transcript_end"],
            run["retained"]["removed_matches"],
            run["execution_records_kept"],
            run["journal_cleared"],
        ]
    return {"fixture": first, "second_fixture": second}, all(checks)


def _rejection() -> tuple[dict, bool]:
    fx = fixture("first")
    world = World()
    with world.clocked():
        stale, _ = world.controller()
        assert stale.acquire()
        old, replacement = world.attempts(stale, fx)
        args = (old.execution_id, fx["state"], fx["transcript_end"], fx["outcome_ref"])
        retention.finalize(world.store, SLUG, stale.require, *args)
        world.acknowledge(old.execution_id, fx["pending_archive_watermark"])
        waiting = world.cleanup(stale).run(old.execution_id).state
        deletes_while_archive_pending = len(world.api.deletes)
        world.acknowledge(old.execution_id, fx["transcript_end"])
        old_name = f"swarm-{old.execution_id}"
        old_pod = copy.deepcopy(world.api.objects[old_name])
        world.api.drop_next_delete = True
        try:
            world.cleanup(stale).run(old.execution_id)
            interrupted = "finished"
        except TimeoutError as error:
            interrupted = str(error)
        reused = world.api.put(old_pod)
        world.clock[0] += kub02.lease.ttl_ms()
        current, _ = world.controller()
        assert current.acquire()
        accepted = retention.final(world.store, SLUG, old.execution_id)
        protected = world.snapshot()
        refusals = {}
        for label, action in (
            ("stale_controller", lambda: world.cleanup(stale).run(old.execution_id)),
            ("stale_finalize", lambda: retention.finalize(world.store, SLUG, stale.require, *args)),
            ("not_final_replacement", lambda: world.cleanup(current).run(replacement.execution_id)),
            (
                "unknown_attempt",
                lambda: retention.finalize(world.store, SLUG, current.require, "exe-" + "f" * 32, "completed", 1),
            ),
            (
                "running_is_not_final",
                lambda: retention.finalize(world.store, SLUG, current.require, old.execution_id, "running", 1),
            ),
            (
                "final_record_is_immutable",
                lambda: retention.finalize(
                    world.store, SLUG, current.require, old.execution_id, "cancelled", fx["transcript_end"]
                ),
            ),
        ):
            try:
                refusals[label] = action().state
            except SwarmError as error:
                refusals[label] = str(error)
        world.grant["allowed"] = False
        try:
            world.cleanup(current).run(old.execution_id)
            refusals["revoked_grant"] = "ran"
        except SwarmError as error:
            refusals["revoked_grant"] = str(error)
        unchanged = world.snapshot() == protected
        final_unchanged = retention.final(world.store, SLUG, old.execution_id) == accepted
        deletes_before_resume = len(world.api.deletes)
        world.grant["allowed"] = True
        resumed = world.cleanup(current).run(old.execution_id)
        private = (kub02.launch()["provider_account"], old.execution_id, replacement.execution_id, old.name)
        observed = {
            "waiting": waiting,
            "deletes_while_archive_pending": deletes_while_archive_pending,
            "interrupted": interrupted,
            "refusals": refusals,
            "protected_state_unchanged_by_refusals": unchanged,
            "final_record_unchanged_by_refusals": final_unchanged,
            "refusals_disclose_nothing": not any(value in json.dumps(refusals) for value in private),
            "deletes_before_resume": deletes_before_resume,
            "resumed": resumed.state,
            "removed": resumed.removed,
            "deletes": world.api.deletes,
            "reused_name_pod_survives": world.api.objects.get(old_name, {}).get("metadata", {}).get("uid")
            == reused["metadata"]["uid"],
            "replacement_pod_survives": f"swarm-{replacement.execution_id}" in world.api.objects,
            "execution_cleanup_backlog": retention.execution_cleanup_backlog(world.store, SLUG),
        }
    observed = _masked(observed, old, replacement)
    checks = [
        observed["waiting"] == "waiting_archive",
        observed["deletes_while_archive_pending"] == 0,
        observed["interrupted"] == "delete response dropped",
        observed["refusals"]
        == {
            "stale_controller": "the controller lease is stale",
            "stale_finalize": "the controller lease is stale",
            "not_final_replacement": "not_final",
            "unknown_attempt": "unknown execution identity; display labels cannot identify attempts",
            "running_is_not_final": "an attempt is final only when completed or explicitly cancelled",
            "final_record_is_immutable": "a final attempt record cannot change",
            "revoked_grant": "a scoped controller grant is required",
        },
        observed["protected_state_unchanged_by_refusals"],
        observed["final_record_unchanged_by_refusals"],
        observed["refusals_disclose_nothing"],
        observed["deletes_before_resume"] == 1,
        observed["resumed"] == "done",
        observed["removed"]
        == {
            "pods": [{"name": "swarm-<old>", "uid": "uid-1", "outcome": "replaced"}],
            "services": [{"name": "swarm-attach-<old>", "uid": "uid-2", "outcome": "deleted"}],
        },
        observed["deletes"] == [["pods", "swarm-<old>", "uid-1"], ["services", "swarm-attach-<old>", "uid-2"]],
        observed["reused_name_pod_survives"],
        observed["replacement_pod_survives"],
        observed["execution_cleanup_backlog"] == {"waiting_outcome": 0, "waiting_archive": 0, "ready": 0},
    ]
    return observed, all(checks)


def _recovery() -> tuple[dict, bool]:
    fx = fixture("first")
    world = World()
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        old, replacement = world.attempts(controller, fx)
        args = (old.execution_id, fx["state"], fx["transcript_end"], fx["outcome_ref"])
        retention.finalize(world.store, SLUG, controller.require, *args)
        world.acknowledge(old.execution_id, fx["pending_archive_watermark"])
        waiting = world.cleanup(controller).run(old.execution_id).state
        nothing_retained_while_waiting = retention.retained(world.store, SLUG, old.execution_id) is None
        world.acknowledge(old.execution_id, fx["transcript_end"])
        accepted = world.snapshot()
        suspended = world.cleanup(controller, enabled=False).run(old.execution_id).state
        retained_while_suspended = world.snapshot() == accepted
        backlog_suspended = retention.execution_cleanup_backlog(world.store, SLUG)
        world.api.drop_next_delete = True
        crashes = []
        try:
            world.cleanup(controller).run(old.execution_id)
        except TimeoutError as error:
            crashes.append(str(error))
        journal = json.loads(world.store.redis.hget(world.store.key(SLUG, "cleanup-journal"), old.execution_id))
        world.secrets.fail_next = True
        try:
            world.cleanup(controller).run(old.execution_id)
        except ConnectionError as error:
            crashes.append(str(error))
        mid_release = json.loads(world.store.redis.hget(world.store.key(SLUG, "cleanup-journal"), old.execution_id))
        world.clock[0] += kub02.lease.ttl_ms()
        restarted, _ = world.controller()
        assert restarted.acquire()
        resumed = world.cleanup(restarted).run(old.execution_id)
        replay = world.cleanup(restarted).run(old.execution_id)
        kept = retention.retained(world.store, SLUG, old.execution_id)
        try:
            world.cleanup(controller).run(old.execution_id)
            earlier_attempt = "ran"
        except SwarmError as error:
            earlier_attempt = str(error)
        observed = {
            "waiting": waiting,
            "nothing_retained_while_waiting": nothing_retained_while_waiting,
            "suspended": suspended,
            "retained_while_suspended": retained_while_suspended,
            "backlog_while_suspended": backlog_suspended,
            "crashes": crashes,
            "journal_after_lost_delete": {
                "pinned": journal["pinned"],
                "sent": journal["sent"],
                "done": journal["done"],
            },
            "journal_mid_release": {"done": mid_release["done"], "removed": mid_release["removed"]},
            "resumed": resumed.state,
            "replay": replay.state,
            "earlier_controller": earlier_attempt,
            "retained_unchanged_after_earlier_controller": retention.retained(world.store, SLUG, old.execution_id)
            == kept,
            "final_record_survived": retention.final(world.store, SLUG, old.execution_id)
            == retention.Final(old.execution_id, old.generation, *args[1:]),
            "removed": resumed.removed,
            "deletes": world.api.deletes,
            "releases": world.log,
            "pods_left": _names(world.api.objects),
            "services_left": _names(world.api.services),
            "attached": sorted(world.attached),
            "secret_refs": sorted(world.secret_refs),
            "retained_once": kept["removed"] == resumed.removed,
            "execution_cleanup_backlog": retention.execution_cleanup_backlog(world.store, SLUG),
        }
    observed = _masked(observed, old, replacement)
    checks = [
        observed["waiting"] == "waiting_archive",
        observed["nothing_retained_while_waiting"],
        observed["suspended"] == "suspended",
        observed["retained_while_suspended"],
        observed["backlog_while_suspended"] == {"waiting_outcome": 0, "waiting_archive": 0, "ready": 1},
        observed["crashes"] == ["delete response dropped", "secrets registry unavailable"],
        observed["journal_after_lost_delete"]
        == {
            "pinned": {
                "pods": [{"name": "swarm-<old>", "uid": "uid-1"}],
                "services": [{"name": "swarm-attach-<old>", "uid": "uid-2"}],
            },
            "sent": ["uid-1"],
            "done": [],
        },
        observed["journal_mid_release"]["done"] == ["pods", "services", "attach"],
        observed["journal_mid_release"]["removed"]
        == {
            "pods": [{"name": "swarm-<old>", "uid": "uid-1", "outcome": "absent_after_send"}],
            "services": [{"name": "swarm-attach-<old>", "uid": "uid-2", "outcome": "deleted"}],
        },
        observed["resumed"] == observed["replay"] == "done",
        observed["earlier_controller"] == "the controller lease is stale",
        observed["retained_unchanged_after_earlier_controller"],
        observed["final_record_survived"],
        observed["deletes"] == [["pods", "swarm-<old>", "uid-1"], ["services", "swarm-attach-<old>", "uid-2"]],
        observed["releases"] == ["attach", "secrets", "secrets"],
        observed["pods_left"] == ["swarm-<replacement>"],
        observed["services_left"] == ["swarm-attach-<replacement>"],
        observed["attached"] == observed["secret_refs"] == ["<replacement>"],
        observed["retained_once"],
        observed["execution_cleanup_backlog"] == {"waiting_outcome": 0, "waiting_archive": 0, "ready": 0},
    ]
    return observed, all(checks)


def run_case(case: str) -> dict:
    observed, passed = {"a": _positive, "b": _rejection, "c": _recovery}[case]()
    return {
        "case": f"T-SV2-KUB-04-{case.upper()}",
        "state": "passed" if passed else "failed",
        "evidence_class": EVIDENCE_CLASS,
        "input_sha256": {name: hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() for name in INPUTS},
        "observed": observed,
    }
