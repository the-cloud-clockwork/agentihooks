import copy
import hashlib
import json
from dataclasses import asdict

from scripts.swarm.store import AgentRecord, SwarmError
from scripts.swarm_v2 import accounts
from scripts.swarm_v2.kubernetes import failures, watch
from scripts.swarm_v2.kubernetes.failures import AccountSlot, Recovery
from tests import sv2_kub02_cases as kub02
from tests import sv2_kub04_cases as kub04

FIXTURES = kub04.FIXTURES
INPUTS = ("pod-failures.json", "pod-policy.json", "pod-launch.json")
SLUG = kub02.SLUG
ACCOUNT = "fixture-account"
EVIDENCE_CLASS = (
    "mocked: fakeredis execution registry, heartbeat archive watermarks, provider account slots, fence and recovery "
    "records, a fake lease clock, an in process API server whose Pods carry fixture statuses, an in process grant "
    "registry and an in process checkpoint list; no Kubernetes API server, node or worker Pod was used"
)


def fixture() -> dict:
    return json.loads((FIXTURES / "pod-failures.json").read_text())


class Checkpoints:
    def __init__(self) -> None:
        self.by_execution: dict[str, list] = {}

    def list(self, execution_id: str) -> list[dict]:
        return copy.deepcopy(self.by_execution.get(execution_id, []))


class World(kub04.World):
    def __init__(self) -> None:
        super().__init__()
        self.fx = fixture()
        self.ready = set(self.fx["nodes"])
        self.grants: set[str] = set()
        self.revocations = kub04.Registry("grant", self.log, self.grants)
        self.checkpoints = Checkpoints()

    def recovery(self, controller, automatic: bool = True) -> Recovery:
        capacity = accounts.AccountCapacity(self.client(), SLUG, lambda token: None, lambda: self.clock[0])
        releases = {"grant": self.revocations, "account": AccountSlot(self.client(), SLUG, capacity)}
        return Recovery(
            self.client(), SLUG, controller, self.checkpoints, releases, self.fx["compatibility"], automatic
        )

    def launch(self, controller, run: dict, seat: str = "eng-1") -> AgentRecord:
        record = AgentRecord(
            self.store.next_name(SLUG, "eng"),
            "eng",
            "task",
            account=ACCOUNT,
            seat=f"{seat}@{SLUG}",
            conversation_id=run["native_session"],
            runtime_backend="kubernetes",
            runtime_target={"pod_namespace": self.api.namespace, "pod_name": "swarm-pending"},
        )
        attempt = controller.admit(record)
        controller.execute(self.request(controller, attempt))
        self.show(attempt, "running", run["node"])
        self.acknowledge(attempt.execution_id, run["archive_watermark"])
        self.grants.add(attempt.execution_id)
        self.checkpoints.by_execution[attempt.execution_id] = run["checkpoints"]
        slot = accounts.Slot(
            ACCOUNT,
            f"{SLUG}/{attempt.seat}",
            attempt.execution_id,
            attempt.generation,
            accounts.RESERVED,
            self.clock[0] + accounts.MAX_RESERVATION_MS,
        )
        self.store.redis.hset(accounts.account_key(ACCOUNT), slot.holder, accounts.encode(slot))
        return attempt

    def show(self, attempt: AgentRecord, shape: str, node: str = "") -> None:
        pod = self.api.objects[f"swarm-{attempt.execution_id}"]
        example = self.fx["pods"][shape]
        pod["status"] = copy.deepcopy(example["status"])
        pod["spec"]["nodeName"] = node or example["spec"]["nodeName"]

    def reconcile(self, recovery: Recovery) -> dict:
        return recovery.reconcile(copy.deepcopy(list(self.api.objects.values())), sorted(self.ready))

    def slots(self) -> list[str]:
        capacity = accounts.AccountCapacity(self.client(), SLUG, lambda token: None, lambda: self.clock[0])
        return [slot.execution_id for slot in capacity.slots(ACCOUNT)]

    def attempts(self, seat: str = "eng-1") -> list[list]:
        return [[agent.execution_id, agent.generation] for agent in self.store.executions(SLUG, f"{seat}@{SLUG}")]

    def occupant(self, seat: str = "eng-1") -> AgentRecord:
        return self.store.execution_occupants(SLUG)[f"{seat}@{SLUG}"]

    def protected(self) -> dict:
        hashes = ("executions", "attempt-fences", "attempt-recoveries")
        return {
            "hashes": {name: self.store.redis.hgetall(self.store.key(SLUG, name)) for name in hashes},
            "pods": copy.deepcopy(self.api.objects),
            "slots": self.slots(),
            "grants": sorted(self.grants),
        }

    def pod_scope(self) -> list[list]:
        return sorted(
            [
                pod["metadata"]["namespace"],
                pod["metadata"]["labels"][watch.OWNER_LABEL],
                pod["metadata"]["labels"][watch.EXECUTION_LABEL],
            ]
            for pod in self.api.objects.values()
        )


def _refused(action) -> str:
    try:
        action()
    except SwarmError as error:
        return str(error)
    return "accepted"


def _node_deleted(name: str) -> dict:
    world = World()
    run = world.fx[name]
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        old = world.launch(controller, run)
        recovery = world.recovery(controller)
        healthy = world.reconcile(recovery)
        world.ready.discard(run["node"])
        world.api.objects.pop(f"swarm-{old.execution_id}")
        deleted = world.reconcile(recovery)
        repeated = world.reconcile(recovery)
        decision = recovery.decision(old.execution_id)
        replacement = world.store.execution(SLUG, decision.replacement)
        controller.execute(world.request(controller, replacement))
        world.show(replacement, "running", run["replacement_node"])
        settled = world.reconcile(recovery)
        observed = {
            "classified": {
                shape: failures.classify(pod, frozenset({"worker-b"})) for shape, pod in world.fx["pods"].items()
            },
            "reconcile": {"healthy": healthy, "node_deleted": deleted, "repeated": repeated, "running": settled},
            "decision": asdict(decision),
            "fence": recovery.fence(old.execution_id),
            "attempts": world.attempts(),
            "occupant": world.occupant().execution_id,
            "old_state": world.store.execution(SLUG, old.execution_id).state,
            "grants": sorted(world.grants),
            "slots": world.slots(),
            "releases": world.log,
            "pod_scope": world.pod_scope(),
            "execution_failures_by_reason": recovery.execution_failures_by_reason(),
        }
    return kub04._masked(observed, old, replacement)


def _positive() -> tuple[dict, bool]:
    first, second = _node_deleted("first"), _node_deleted("second")
    fx, checks = fixture(), []
    for run, name, checkpoint in ((first, "first", "ckpt-one-2"), (second, "second", "ckpt-two-1")):
        source = fx[name]
        checks += [
            run["classified"]
            == {
                "oom_killed": "oom_killed",
                "evicted": "evicted",
                "node_unreachable": "node_lost",
                "node_lost": "node_lost",
                "image_pull": "image_pull",
                "init_image_pull": "image_pull",
                "application_exit": "application_exit",
                "clean_exit": "",
                "running": "",
            },
            run["reconcile"]
            == {
                "healthy": {"<old>": "working"},
                "node_deleted": {"<old>": "fenced"},
                "repeated": {"<replacement>": "unobserved"},
                "running": {"<replacement>": "working"},
            },
            run["decision"]
            == {
                "execution_id": "<old>",
                "generation": 1,
                "reason": "node_lost",
                "mode": "resume",
                "checkpoint": checkpoint,
                "replacement": "<replacement>",
            },
            run["fence"]["reason"] == "node_lost",
            run["fence"]["native_session"] == source["native_session"],
            run["fence"]["archive_watermark"] == source["archive_watermark"],
            run["fence"]["tail"] == failures.tail("node_lost", source["native_session"], source["archive_watermark"]),
            run["fence"]["released"] == ["grant", "account"],
            run["attempts"] == [["<old>", 1], ["<replacement>", 2]],
            run["occupant"] == "<replacement>",
            run["old_state"] == "fenced",
            run["grants"] == [],
            run["slots"] == [],
            run["releases"] == ["grant"],
            run["pod_scope"] == [[kub02.policy()["namespace"], watch.owner_for(SLUG), "<replacement>"]],
            run["execution_failures_by_reason"] == {**dict.fromkeys(failures.REASONS, 0), "node_lost": 1},
        ]
    return {"fixture": first, "second_fixture": second}, all(checks)


def _rejection() -> tuple[dict, bool]:
    world = World()
    run = world.fx["first"]
    with world.clocked():
        stale, _ = world.controller()
        assert stale.acquire()
        old = world.launch(stale, run)
        world.show(old, "image_pull")
        world.checkpoints.by_execution[old.execution_id] = [
            entry for entry in run["checkpoints"] if entry["checkpoint_id"] in ("ckpt-one-3", "ckpt-one-4")
        ]
        recovery = world.recovery(stale)
        start, pulls = world.clock[0], []
        for offset in (0, failures.PULL_GRACE_MS - 1, failures.PULL_GRACE_MS):
            world.clock[0] = start + offset
            pulls.append([offset, world.reconcile(recovery), world.slots()])
        decision = recovery.decision(old.execution_id)
        pending = {
            "decision": asdict(decision),
            "state": world.occupant().state,
            "tail": recovery.fence(old.execution_id)["tail"],
            "attempts": world.attempts(),
            "grants": sorted(world.grants),
        }
        world.clock[0] += kub02.lease.ttl_ms()
        current, _ = world.controller()
        assert current.acquire()
        live = world.recovery(current)
        protected = world.protected()
        refusals = {
            "stale_controller": _refused(lambda: world.recovery(stale).decide(old.execution_id, "fresh")),
            "unsupported_reason": _refused(lambda: live.handle(old.execution_id, "cosmic_ray")),
            "unknown_choice": _refused(lambda: live.decide(old.execution_id, "maybe")),
            "resume_without_checkpoint": _refused(lambda: live.decide(old.execution_id, "resume")),
            "nothing_pending": _refused(lambda: live.decide("exe-" + "f" * 32, "fresh")),
        }
        world.grant["allowed"] = False
        refusals["revoked_grant"] = _refused(lambda: live.decide(old.execution_id, "fresh"))
        world.grant["allowed"] = True
        unchanged = world.protected() == protected
        fresh = live.decide(old.execution_id, "fresh")
        again = _refused(lambda: live.decide(old.execution_id, "fresh"))
        observed = {
            "pulls": pulls,
            "pending": pending,
            "refusals": refusals,
            "protected_state_unchanged_by_refusals": unchanged,
            "fresh": asdict(fresh),
            "repeated_decision": again,
            "attempts": world.attempts(),
            "execution_failures_by_reason": live.execution_failures_by_reason(),
        }
    observed = kub04._masked(observed, old, world.store.execution(SLUG, fresh.replacement))
    grace = failures.PULL_GRACE_MS
    checks = [
        observed["pulls"]
        == [
            [0, {"<old>": "pulling"}, ["<old>"]],
            [grace - 1, {"<old>": "pulling"}, ["<old>"]],
            [grace, {"<old>": "fenced"}, []],
        ],
        observed["pending"]
        == {
            "decision": {
                "execution_id": "<old>",
                "generation": 1,
                "reason": "image_pull",
                "mode": "recovery_pending",
                "checkpoint": "",
                "replacement": "",
            },
            "state": "awaiting-decision",
            "tail": failures.NO_TAIL,
            "attempts": [["<old>", 1]],
            "grants": [],
        },
        observed["refusals"]
        == {
            "stale_controller": "the controller lease is stale",
            "unsupported_reason": "unsupported failure reason",
            "unknown_choice": "a recovery decision is fresh or resume",
            "resume_without_checkpoint": "no complete compatible checkpoint to resume from",
            "nothing_pending": "no recovery decision is pending for this attempt",
            "revoked_grant": "a scoped controller grant is required",
        },
        observed["protected_state_unchanged_by_refusals"],
        observed["fresh"]
        == {
            "execution_id": "<old>",
            "generation": 1,
            "reason": "image_pull",
            "mode": "fresh",
            "checkpoint": "",
            "replacement": "<replacement>",
        },
        observed["repeated_decision"] == "no recovery decision is pending for this attempt",
        observed["attempts"] == [["<old>", 1], ["<replacement>", 2]],
        observed["execution_failures_by_reason"] == {**dict.fromkeys(failures.REASONS, 0), "image_pull": 1},
    ]
    return observed, all(checks)


def _recovery() -> tuple[dict, bool]:
    world = World()
    run = world.fx["first"]
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        old = world.launch(controller, run)
        world.show(old, "oom_killed")
        crashes = []
        world.revocations.fail_next = True
        try:
            world.reconcile(world.recovery(controller))
        except ConnectionError as error:
            crashes.append(str(error))
        fenced = world.recovery(controller).fence(old.execution_id)
        world.clock[0] += kub02.lease.ttl_ms()
        restarted, _ = world.controller()
        assert restarted.acquire()
        admit = restarted.admit

        def lost(record, previous_execution_id=""):
            admit(record, previous_execution_id)
            raise TimeoutError("admission response dropped")

        restarted.admit = lost
        try:
            world.reconcile(world.recovery(restarted))
        except TimeoutError as error:
            crashes.append(str(error))
        restarted.admit = admit
        recovery = world.recovery(restarted)
        resumed = world.reconcile(recovery)
        decision = recovery.decision(old.execution_id)
        replacement = world.store.execution(SLUG, decision.replacement)
        old_record = world.store.execution(SLUG, old.execution_id)
        world.show(old, "running")
        late = [world.reconcile(recovery), world.reconcile(recovery)]
        late_count = recovery.late_observations(old.execution_id)
        paused = world.recovery(restarted, automatic=False)
        other = world.launch(restarted, world.fx["second"], "eng-2")
        world.show(other, "evicted")
        paused_first = world.reconcile(paused)
        world.show(other, "running")
        paused_late = world.reconcile(paused)
        observed = {
            "crashes": crashes,
            "fence_after_crash": {"released": fenced["released"], "reason": fenced["reason"]},
            "fence_written_once": recovery.fence(old.execution_id)["fenced_at_ms"] == fenced["fenced_at_ms"],
            "resumed": resumed,
            "decision": asdict(decision),
            "attempts": world.attempts(),
            "occupant": world.occupant().execution_id,
            "late_old_pod": late,
            "late_observations": late_count,
            "old_record_unchanged": world.store.execution(SLUG, old.execution_id) == old_record,
            "old_state": old_record.state,
            "decision_unchanged": recovery.decision(old.execution_id) == decision,
            "releases": world.log,
            "slots": world.slots(),
            "rollback": {
                "first": paused_first,
                "late": paused_late,
                "decision": asdict(paused.decision(other.execution_id)),
                "state": world.occupant("eng-2").state,
                "attempts": len(world.attempts("eng-2")),
            },
            "execution_failures_by_reason": recovery.execution_failures_by_reason(),
        }
    observed = json.loads(json.dumps(kub04._masked(observed, old, replacement)).replace(other.execution_id, "<other>"))
    checks = [
        observed["crashes"] == ["grant registry unavailable", "admission response dropped"],
        observed["fence_after_crash"] == {"released": [], "reason": "oom_killed"},
        observed["fence_written_once"],
        observed["resumed"] == {"<replacement>": "unobserved", "<old>": "retired"},
        observed["decision"]
        == {
            "execution_id": "<old>",
            "generation": 1,
            "reason": "oom_killed",
            "mode": "resume",
            "checkpoint": "ckpt-one-2",
            "replacement": "<replacement>",
        },
        observed["attempts"] == [["<old>", 1], ["<replacement>", 2]],
        observed["occupant"] == "<replacement>",
        observed["late_old_pod"] == [{"<replacement>": "unobserved", "<old>": "retired"}] * 2,
        observed["late_observations"] == 3,
        observed["old_record_unchanged"],
        observed["old_state"] == "fenced",
        observed["decision_unchanged"],
        observed["releases"] == ["grant", "grant", "grant"],
        observed["slots"] == [],
        observed["rollback"]
        == {
            "first": {"<replacement>": "unobserved", "<other>": "fenced", "<old>": "retired"},
            "late": {"<replacement>": "unobserved", "<other>": "retired", "<old>": "retired"},
            "decision": {
                "execution_id": "<other>",
                "generation": 1,
                "reason": "evicted",
                "mode": "recovery_pending",
                "checkpoint": "",
                "replacement": "",
            },
            "state": "awaiting-decision",
            "attempts": 1,
        },
        observed["execution_failures_by_reason"]
        == {**dict.fromkeys(failures.REASONS, 0), "oom_killed": 1, "evicted": 1},
    ]
    return observed, all(checks)


def run_case(case: str) -> dict:
    observed, passed = {"a": _positive, "b": _rejection, "c": _recovery}[case]()
    return {
        "case": f"T-SV2-KUB-05-{case.upper()}",
        "state": "passed" if passed else "failed",
        "evidence_class": EVIDENCE_CLASS,
        "input_sha256": {name: hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() for name in INPUTS},
        "observed": observed,
    }
