"""Abrupt Pod and node failures become fenced recovery decisions: the old attempt is fenced before any replacement."""

import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, replace
from typing import Protocol

from scripts.swarm import lease
from scripts.swarm.store import AgentRecord, RedisStore, SwarmError
from scripts.swarm_v2 import retention
from scripts.swarm_v2.accounts import AccountCapacity
from scripts.swarm_v2.checkpoints import model
from scripts.swarm_v2.checkpoints import restore as restoring
from scripts.swarm_v2.controller import Controller
from scripts.swarm_v2.kubernetes.cleanup import Release
from scripts.swarm_v2.kubernetes.watch import BACKEND, EXECUTION_LABEL
from scripts.swarm_v2.runtime import observe
from scripts.swarm_v2.runtime.operations import SPAWN, Phase

OOM_KILLED, EVICTED, NODE_LOST = "oom_killed", "evicted", "node_lost"
IMAGE_PULL, APPLICATION_EXIT = "image_pull", "application_exit"
REASONS = (OOM_KILLED, EVICTED, NODE_LOST, IMAGE_PULL, APPLICATION_EXIT)
LEGACY_SPEC = "missing_spec_digest"
PULL_WAITING = frozenset(("ErrImagePull", "ImagePullBackOff", "InvalidImageName"))
PULL_GRACE_MS = 120_000
NODE_NOT_READY = "NodeNotReady"
NODE_LOST_REASON = "NodeLost"
DISRUPTION = "DisruptionTarget"
NODE_DISRUPTIONS = frozenset(("DeletionByTaintManager", "DeletionByPodGC"))
RELEASES = ("grant", "account")
RESUME, FRESH, PENDING = "resume", "fresh", "recovery_pending"
AWAITING, FENCED = "awaiting-decision", "fenced"
PENDING_POD = "swarm-pending"
NEW_ATTEMPT = {"execution_id": "", "generation": 0, "state": "working", "conversation_id": ""}
FENCES = "attempt-fences"
RECOVERIES = "attempt-recoveries"
LATE = "attempt-late-observations"
NODES = "attempt-nodes"
PULLS = "image-pull-since"
PAUSED = "recovery-paused"
RESTORES = "attempt-restores"
REQUESTS = "recovery-requests"
NO_TAIL = "the image never started, so no transcript was written"
TAIL = (
    "transcript written by native session {session} after archive offset {watermark} was not archived before the "
    "{reason} failure and is not recovered; its length is unknown"
)


class Checkpoints(Protocol):
    def list(self, execution_id: str) -> list[dict]:
        """Each entry carries checkpoint_id, sequence, status and compatibility."""
        ...


@dataclass(frozen=True)
class Decision:
    execution_id: str
    generation: int
    reason: str
    mode: str
    checkpoint: str = ""
    replacement: str = ""


def classify(pod: dict, ready: frozenset[str] | None = None) -> str:
    """Empty when the Pod shows no failure; `ready` names the Ready nodes, or None when no node listing succeeded."""
    status = pod.get("status", {})
    containers = (*status.get("initContainerStatuses", []), *status.get("containerStatuses", []))
    states = [container.get("state", {}) for container in containers]
    exits = [state["terminated"] for state in states if "terminated" in state]
    if any(done.get("reason") == "OOMKilled" for done in exits):
        return OOM_KILLED
    if status.get("reason") == "Evicted":
        return EVICTED
    if _node_lost(status, pod.get("spec", {}).get("nodeName"), ready):
        return NODE_LOST
    if any(state.get("waiting", {}).get("reason") in PULL_WAITING for state in states):
        return IMAGE_PULL
    if status.get("phase") == "Failed" or any(done.get("exitCode") != 0 for done in exits):
        return APPLICATION_EXIT
    return ""


def _node_lost(status: dict, node: str | None, ready: frozenset[str] | None) -> bool:
    if status.get("reason") == NODE_LOST_REASON:
        return True
    if any(_node_condition(condition) for condition in status.get("conditions", [])):
        return True
    return ready is not None and bool(node) and node not in ready


def _node_condition(condition: dict) -> bool:
    if condition.get("reason") in (NODE_NOT_READY, NODE_LOST_REASON):
        return True
    disrupted = condition.get("type") == DISRUPTION and condition.get("status") == "True"
    return disrupted and condition.get("reason") in NODE_DISRUPTIONS


def deleted_reason(pod: dict, ready: frozenset[str] | None) -> str:
    """A deleted Pod of a current attempt that had a node and shows no other failure lost it with that node."""
    reason = classify(pod, ready)
    if reason or pod.get("status", {}).get("phase") == "Succeeded":
        return reason
    return NODE_LOST if pod.get("spec", {}).get("nodeName") else ""


def tail(reason: str, session: str, watermark: int) -> str:
    if reason == IMAGE_PULL:
        return NO_TAIL
    return TAIL.format(session=session or "unknown", watermark=watermark, reason=reason.replace("_", " "))


def restore_directive(store: RedisStore, slug: str, replacement: str) -> dict | None:
    """The restore a replacement attempt's launch record names, or None for an attempt no recovery admitted."""
    raw = store.redis.hget(store.key(slug, RESTORES), replacement)
    return json.loads(raw) if raw else None


class AccountSlot:
    """Releases the provider slot held by exactly the fenced execution and generation."""

    def __init__(self, store: RedisStore, slug: str, capacity: AccountCapacity) -> None:
        self.store, self.slug, self.capacity = store, slug, capacity

    def release(self, execution_id: str) -> None:
        agent = self.store.execution(self.slug, execution_id)
        if agent.account:
            self.capacity.end(agent.account, agent.seat, agent.execution_id, agent.generation)


class Recovery:
    """Every mutation runs under `controller.require`: a scoped controller grant and the current lease."""

    def __init__(
        self,
        store: RedisStore,
        slug: str,
        controller: Controller,
        checkpoints: Checkpoints,
        releases: Mapping[str, Release],
        compatibility: str,
        automatic: bool = True,
    ) -> None:
        self.store, self.slug, self.controller = store, slug, controller
        self.checkpoints, self.releases = checkpoints, releases
        self.compatibility, self.automatic = compatibility, automatic

    def reconcile(
        self, pods: Iterable[dict], ready_nodes: Iterable[str] | None, deleted: Iterable[dict] = ()
    ) -> dict[str, str]:
        """Pass None for `ready_nodes` when the node listing failed, so an unlisted node is never proof of loss;
        `deleted` holds the last state of each Pod deleted since the previous pass."""
        self.controller.require()
        self._legacy_spawns()
        self._resume()
        self._requested()
        ready = None if ready_nodes is None else frozenset(ready_nodes)
        groups, seen = {}, {}
        for pod in pods:
            execution_id = pod["metadata"].get("labels", {}).get(EXECUTION_LABEL)
            if execution_id:
                groups.setdefault(execution_id, []).append(pod)
        gone = {pod["metadata"].get("labels", {}).get(EXECUTION_LABEL): pod for pod in deleted}
        for agent in self.store.execution_occupants(self.slug).values():
            if agent.runtime_backend == BACKEND:
                group = groups.pop(agent.execution_id, [])
                seen[agent.execution_id] = self._observe(agent, group, ready, gone.get(agent.execution_id))
        for execution_id, group in groups.items():
            if self.fence(execution_id):
                seen[execution_id] = self._late(execution_id, len(group))
        return seen

    def handle(self, execution_id: str, reason: str) -> Decision:
        if reason not in (*REASONS, LEGACY_SPEC):
            raise SwarmError("unsupported failure reason")
        self.controller.require()
        known = self.decision(execution_id)
        if known:
            return known
        agent = self.store.execution(self.slug, execution_id)
        fence = self._fence(agent, reason)
        for step in RELEASES:
            if step not in fence["released"]:
                self.controller.require()
                self.releases[step].release(execution_id)
                fence["released"].append(step)
                self._write(FENCES, execution_id, fence)
        return self._decide(agent, fence["reason"])

    def decide(self, execution_id: str, choice: str) -> Decision:
        if choice not in (FRESH, RESUME):
            raise SwarmError("a recovery decision is fresh or resume")
        self.controller.require()
        known = self.decision(execution_id)
        if known is None or known.mode != PENDING:
            raise SwarmError("no recovery decision is pending for this attempt")
        entry = self._checkpoint(execution_id) if choice == RESUME else {}
        if choice == RESUME and not entry:
            raise SwarmError("no complete compatible checkpoint to resume from")
        agent = self.store.execution(self.slug, execution_id)
        native = entry.get("continuation") != model.CONTEXT and bool(agent.conversation_id)
        recovery = restoring.FRESH if choice == FRESH else restoring.NATIVE if native else restoring.RECONSTRUCTED
        successor = self._replace(agent)
        checkpoint = entry.get("checkpoint_id", "")
        decision = replace(known, mode=choice, checkpoint=checkpoint, replacement=successor.execution_id)
        self._direct(decision, recovery)
        return self._record(agent, decision, successor.generation)

    def request(self, execution_id: str, choice: str) -> Decision:
        """A repeated request for the choice already accepted returns that decision and admits nothing."""
        known = self.decision(execution_id)
        if choice in (FRESH, RESUME) and known is not None and known.mode == choice:
            self.controller.require()
            return known
        return self.decide(execution_id, choice)

    def restore(self, replacement: str) -> dict | None:
        return restore_directive(self.store, self.slug, replacement)

    def outcome(self, replacement: str) -> dict | None:
        raw = self.store.redis.hget(self._key("attempt-restore-outcomes"), replacement)
        return json.loads(raw) if raw else None

    def confirm(self, replacement: str, restored: dict) -> dict:
        self.controller.require()
        agent = self.store.execution(self.slug, replacement)
        occupant = self.store.execution_occupants(self.slug)[agent.seat]
        if occupant.execution_id != replacement or occupant.generation != agent.generation:
            raise SwarmError("restore outcome is for a superseded execution")
        directive = self.restore(replacement)
        if (
            directive is None
            or not isinstance(restored, dict)
            or set(restored) != set(restoring.Restored.__dataclass_fields__)
        ):
            raise SwarmError("no restore directive for this attempt")
        expected = (directive["recovery"], directive["checkpoint_id"], directive["session_id"])
        actual = (restored.get("recovery"), restored.get("checkpoint_id"), restored.get("session_id"))
        if actual != expected or type(restored.get("watermark")) is not int or restored["watermark"] < 0:
            raise SwarmError("restore outcome does not match the accepted directive")
        command = restored["command"]
        if not isinstance(command, (list, tuple)) or any(not isinstance(word, str) for word in command):
            raise SwarmError("restore command must be a sequence of strings")
        outcome = {
            **restored,
            "command": list(command),
            "status": "verified",
            "archive_watermark": directive["watermark"],
        }
        known = self.outcome(replacement)
        if known is not None and known != outcome:
            raise SwarmError("restore outcome is already recorded")
        self.store.redis.hsetnx(self._key("attempt-restore-outcomes"), replacement, json.dumps(outcome))
        return self.outcome(replacement)

    def restore_outcomes_by_type(self) -> dict[str, int]:
        kinds = [json.loads(raw)["recovery"] for raw in self.store.redis.hvals(self._key("attempt-restore-outcomes"))]
        return {kind: kinds.count(kind) for kind in restoring.TYPES}

    def _direct(self, decision: Decision, recovery: str) -> None:
        fence = self.fence(decision.execution_id)
        session = fence["native_session"] if recovery == restoring.NATIVE else ""
        directive = restoring.Directive(decision.checkpoint, recovery, session, fence["archive_watermark"])
        self.controller.require()
        self.store.redis.hset(self._key(RESTORES), decision.replacement, json.dumps(directive.record()))

    def _requested(self) -> None:
        for execution_id, choice in sorted(self.store.redis.hgetall(self._key(REQUESTS)).items()):
            try:
                self.request(execution_id, choice)
            except SwarmError as error:
                print(f"controller: the recovery request for {execution_id} was refused: {error}", flush=True)
            self.controller.require()
            self.store.redis.hdel(self._key(REQUESTS), execution_id)

    def pause(self, paused: bool) -> None:
        """The operator's pause outlives a controller restart; the constructor flag stays the deployment default."""
        self.controller.require()
        if paused:
            self.store.redis.set(self._key(PAUSED), self.controller.held.epoch)
        else:
            self.store.redis.delete(self._key(PAUSED))

    def paused(self) -> bool:
        return bool(self.store.redis.exists(self._key(PAUSED)))

    def fence(self, execution_id: str) -> dict | None:
        raw = self.store.redis.hget(self._key(FENCES), execution_id)
        return json.loads(raw) if raw else None

    def decision(self, execution_id: str) -> Decision | None:
        raw = self.store.redis.hget(self._key(RECOVERIES), execution_id)
        return Decision(**json.loads(raw)) if raw else None

    def pending(self) -> list[str]:
        decided = self.store.redis.hkeys(self._key(RECOVERIES))
        return sorted(execution_id for execution_id in decided if self.decision(execution_id).mode == PENDING)

    def late_observations(self, execution_id: str) -> int:
        return int(self.store.redis.hget(self._key(LATE), execution_id) or 0)

    def execution_failures_by_reason(self) -> dict[str, int]:
        reasons = [json.loads(raw)["reason"] for raw in self.store.redis.hvals(self._key(FENCES))]
        return {reason: reasons.count(reason) for reason in REASONS}

    def _key(self, name: str) -> str:
        return self.store.key(self.slug, name)

    def _resume(self) -> None:
        decided = set(self.store.redis.hkeys(self._key(RECOVERIES)))
        for execution_id in sorted(set(self.store.redis.hkeys(self._key(FENCES))) - decided):
            self.handle(execution_id, self.fence(execution_id)["reason"])

    def _legacy_spawns(self) -> None:
        occupants = {agent.execution_id for agent in self.store.execution_occupants(self.slug).values()}
        for operation in self.store.operation_journal.records(self.slug):
            if (
                operation.execution_id not in occupants
                or operation.backend != BACKEND
                or operation.action != SPAWN
                or operation.spec_digest
                or operation.phase is Phase.APPLIED
                or retention.final(self.store, self.slug, operation.execution_id)
            ):
                continue
            self.controller.require()
            with lease.fencing(self.controller.held.epoch):
                self.store.operation_journal.supersede_legacy_spawn(self.slug, operation)
            self.handle(operation.execution_id, LEGACY_SPEC)

    def _observe(
        self, agent: AgentRecord, pods: list[dict], ready: frozenset[str] | None, last: dict | None = None
    ) -> str:
        if self.fence(agent.execution_id):
            return self._late(agent.execution_id, len(pods))
        if len(pods) > 1:
            return "ambiguous"
        final = retention.final(self.store, self.slug, agent.execution_id)
        if final:
            return final.state
        reason = self._reason(agent.execution_id, pods[0], ready) if pods else self._vanished(agent, ready, last)
        if reason == IMAGE_PULL and pods and self._pulling(agent.execution_id):
            return "pulling"
        if not reason:
            return "working" if pods else "unobserved"
        self.handle(agent.execution_id, reason)
        return "fenced"

    def _late(self, execution_id: str, count: int) -> str:
        if count:
            self.controller.require()
            self.store.redis.hincrby(self._key(LATE), execution_id, count)
        return "retired"

    def _reason(self, execution_id: str, pod: dict, ready: frozenset[str] | None) -> str:
        node = pod.get("spec", {}).get("nodeName")
        reason = classify(pod, ready)
        self.controller.require()
        if node:
            self.store.redis.hset(self._key(NODES), execution_id, node)
        if reason != IMAGE_PULL:
            self.store.redis.hdel(self._key(PULLS), execution_id)
        return reason

    def _vanished(self, agent: AgentRecord, ready: frozenset[str] | None, last: dict | None) -> str:
        if last:
            return deleted_reason(last, ready)
        node = self.store.redis.hget(self._key(NODES), agent.execution_id)
        if ready is not None and node and node not in ready:
            return NODE_LOST
        return NODE_LOST if self._observed_gone(agent) else ""

    def _observed_gone(self, agent: AgentRecord) -> bool:
        seen = observe.stored(self.store, self.slug, agent.execution_id)
        if seen is None or seen.generation != agent.generation or seen.state is not observe.State.LOST:
            return False
        return seen.sources.get(observe.Source.KUBERNETES, {}).get("reading") == observe.Reading.NOT_FOUND

    def _pulling(self, execution_id: str) -> bool:
        now, key = lease.now_ms(self.store), self._key(PULLS)
        self.controller.require()
        self.store.redis.hsetnx(key, execution_id, now)
        return now - int(self.store.redis.hget(key, execution_id)) < PULL_GRACE_MS

    def _fence(self, agent: AgentRecord, reason: str) -> dict:
        current = self.fence(agent.execution_id)
        if current:
            return current
        occupant = self.store.execution_occupants(self.slug)[agent.seat]
        if occupant.execution_id != agent.execution_id:
            raise SwarmError("only the current attempt of a seat can be fenced")
        watermark = retention.archive_watermark(self.store, self.slug, agent.execution_id)
        fence = {
            "execution_id": agent.execution_id,
            "generation": agent.generation,
            "seat": agent.seat,
            "reason": reason,
            "controller_epoch": self.controller.held.epoch,
            "fenced_at_ms": lease.now_ms(self.store),
            "native_session": agent.conversation_id,
            "archive_watermark": watermark,
            "tail": tail(reason, agent.conversation_id, watermark),
            "released": [],
        }
        self.controller.require()
        if not self.store.redis.hsetnx(self._key(FENCES), agent.execution_id, json.dumps(fence)):
            return self.fence(agent.execution_id)
        self.controller.telemetry.record(
            "fence",
            "accepted",
            0.0,
            task=agent.task,
            seat=agent.seat,
            execution_id=agent.execution_id,
            expected_generation=agent.generation,
            observed_generation=occupant.generation,
            controller_epoch=fence["controller_epoch"],
            reason=reason,
        )
        return fence

    def _decide(self, agent: AgentRecord, reason: str) -> Decision:
        if reason == LEGACY_SPEC and self.automatic and not self.paused():
            successor = self._replace(agent)
            decision = Decision(agent.execution_id, agent.generation, reason, FRESH, replacement=successor.execution_id)
            self._direct(decision, restoring.FRESH)
            return self._record(agent, decision, successor.generation)
        entry = self._checkpoint(agent.execution_id, native_only=True)
        if not self.automatic or self.paused() or reason == IMAGE_PULL or not entry or not agent.conversation_id:
            occupant = self.store.execution_occupants(self.slug)[agent.seat]
            if occupant.execution_id == agent.execution_id:
                self.controller.require()
                self.store.put_agent(self.slug, replace(occupant, state=AWAITING))
            decision = Decision(agent.execution_id, agent.generation, reason, PENDING)
            return self._record(agent, decision, occupant.generation)
        successor = self._replace(agent)
        checkpoint = entry["checkpoint_id"]
        decision = Decision(agent.execution_id, agent.generation, reason, RESUME, checkpoint, successor.execution_id)
        self._direct(decision, restoring.NATIVE)
        return self._record(agent, decision, successor.generation)

    def _checkpoint(self, execution_id: str, native_only: bool = False) -> dict:
        usable = [
            entry
            for entry in self.checkpoints.list(execution_id)
            if entry.get("status") == "complete" and entry.get("compatibility") == self.compatibility
        ]
        if native_only:
            usable = [entry for entry in usable if entry.get("continuation") != model.CONTEXT]
        return max(usable, key=lambda entry: entry["sequence"]) if usable else {}

    def _replace(self, agent: AgentRecord) -> AgentRecord:
        occupant = self.store.execution_occupants(self.slug)[agent.seat]
        if occupant.execution_id != agent.execution_id:
            if occupant.generation != agent.generation + 1:
                raise SwarmError("the seat moved past the fenced attempt")
            return occupant
        self.controller.require()
        self.store.put_agent(self.slug, replace(occupant, state=FENCED))
        target = {"pod_namespace": agent.runtime_target["pod_namespace"], "pod_name": PENDING_POD}
        record = replace(agent, runtime_target=target, **NEW_ATTEMPT)
        return self.controller.admit(record, agent.execution_id)

    def _record(self, agent: AgentRecord, decision: Decision, observed_generation: int) -> Decision:
        self._write("attempt-checkpoint-options", decision.execution_id, self._checkpoint(decision.execution_id))
        self._write(RECOVERIES, decision.execution_id, asdict(decision))
        self.controller.telemetry.record(
            "recovery",
            "accepted",
            0.0,
            task=agent.task,
            seat=agent.seat,
            execution_id=decision.execution_id,
            expected_generation=decision.generation,
            observed_generation=observed_generation,
            controller_epoch=self.controller.held.epoch,
            reason=decision.reason,
            mode=decision.mode,
            replacement=decision.replacement,
        )
        return decision

    def _write(self, name: str, execution_id: str, value: dict) -> None:
        self.controller.require()
        self.store.redis.hset(self._key(name), execution_id, json.dumps(value))
