"""Abrupt Pod and node failures become fenced recovery decisions: the old attempt is fenced before any replacement."""

import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, replace
from typing import Protocol

from scripts.swarm import lease
from scripts.swarm.store import AgentRecord, RedisStore, SwarmError
from scripts.swarm_v2 import retention
from scripts.swarm_v2.accounts import AccountCapacity
from scripts.swarm_v2.controller import Controller
from scripts.swarm_v2.kubernetes.cleanup import Release
from scripts.swarm_v2.kubernetes.watch import BACKEND, EXECUTION_LABEL

OOM_KILLED, EVICTED, NODE_LOST = "oom_killed", "evicted", "node_lost"
IMAGE_PULL, APPLICATION_EXIT = "image_pull", "application_exit"
REASONS = (OOM_KILLED, EVICTED, NODE_LOST, IMAGE_PULL, APPLICATION_EXIT)
PULL_WAITING = frozenset(("ErrImagePull", "ImagePullBackOff", "InvalidImageName"))
PULL_GRACE_MS = 120_000
NODE_NOT_READY = "NodeNotReady"
RELEASES = ("grant", "account")
RESUME, FRESH, PENDING = "resume", "fresh", "recovery_pending"
AWAITING, FENCED = "awaiting-decision", "fenced"
PENDING_POD = "swarm-pending"
FENCES = "attempt-fences"
RECOVERIES = "attempt-recoveries"
LATE = "attempt-late-observations"
NODES = "attempt-nodes"
PULLS = "image-pull-since"
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
    if status.get("reason") == "NodeLost":
        return True
    if any(condition.get("reason") == NODE_NOT_READY for condition in status.get("conditions", [])):
        return True
    return ready is not None and bool(node) and node not in ready


def tail(reason: str, session: str, watermark: int) -> str:
    if reason == IMAGE_PULL:
        return NO_TAIL
    return TAIL.format(session=session or "unknown", watermark=watermark, reason=reason.replace("_", " "))


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

    def reconcile(self, pods: Iterable[dict], ready_nodes: Iterable[str] | None) -> dict[str, str]:
        """Pass None for `ready_nodes` when the node listing failed, so no Pod is judged lost on missing evidence."""
        self.controller.require()
        self._resume()
        ready = None if ready_nodes is None else frozenset(ready_nodes)
        groups, seen = {}, {}
        for pod in pods:
            groups.setdefault(pod["metadata"].get("labels", {}).get(EXECUTION_LABEL, ""), []).append(pod)
        for agent in self.store.execution_occupants(self.slug).values():
            if agent.runtime_backend == BACKEND:
                seen[agent.execution_id] = self._observe(agent, groups.pop(agent.execution_id, []), ready)
        for execution_id, group in groups.items():
            if self.fence(execution_id):
                seen[execution_id] = self._late(execution_id, len(group))
        return seen

    def handle(self, execution_id: str, reason: str) -> Decision:
        if reason not in REASONS:
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
        checkpoint = self._checkpoint(execution_id) if choice == RESUME else ""
        if choice == RESUME and not checkpoint:
            raise SwarmError("no complete compatible checkpoint to resume from")
        replacement = self._replace(self.store.execution(self.slug, execution_id))
        return self._record(replace(known, mode=choice, checkpoint=checkpoint, replacement=replacement))

    def fence(self, execution_id: str) -> dict | None:
        raw = self.store.redis.hget(self._key(FENCES), execution_id)
        return json.loads(raw) if raw else None

    def decision(self, execution_id: str) -> Decision | None:
        raw = self.store.redis.hget(self._key(RECOVERIES), execution_id)
        return Decision(**json.loads(raw)) if raw else None

    def late_observations(self, execution_id: str) -> int:
        return int(self.store.redis.hget(self._key(LATE), execution_id) or 0)

    def execution_failures_by_reason(self) -> dict[str, int]:
        reasons = [json.loads(raw)["reason"] for raw in self.store.redis.hvals(self._key(FENCES))]
        return {reason: reasons.count(reason) for reason in REASONS}

    def _key(self, name: str) -> str:
        return self.store.key(self.slug, name)

    def _resume(self) -> None:
        decided = set(self.store.redis.hkeys(self._key(RECOVERIES)))
        for execution_id in set(self.store.redis.hkeys(self._key(FENCES))) - decided:
            self.handle(execution_id, self.fence(execution_id)["reason"])

    def _observe(self, agent: AgentRecord, pods: list[dict], ready: frozenset[str] | None) -> str:
        if self.fence(agent.execution_id):
            return self._late(agent.execution_id, len(pods))
        if len(pods) > 1:
            return "ambiguous"
        reason = self._reason(agent.execution_id, pods[0], ready) if pods else self._vanished(agent, ready)
        if reason == IMAGE_PULL and self._pulling(agent.execution_id):
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

    def _vanished(self, agent: AgentRecord, ready: frozenset[str] | None) -> str:
        node = self.store.redis.hget(self._key(NODES), agent.execution_id)
        return NODE_LOST if ready is not None and node and node not in ready else ""

    def _pulling(self, execution_id: str) -> bool:
        now, key = lease.now_ms(self.store), self._key(PULLS)
        self.controller.require()
        self.store.redis.hsetnx(key, execution_id, now)
        return now - int(self.store.redis.hget(key, execution_id)) < PULL_GRACE_MS

    def _fence(self, agent: AgentRecord, reason: str) -> dict:
        current = self.fence(agent.execution_id)
        if current:
            return current
        if self.store.execution_occupants(self.slug)[agent.seat].execution_id != agent.execution_id:
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
        return fence

    def _decide(self, agent: AgentRecord, reason: str) -> Decision:
        checkpoint = self._checkpoint(agent.execution_id)
        if not self.automatic or reason == IMAGE_PULL or not checkpoint:
            occupant = self.store.execution_occupants(self.slug)[agent.seat]
            if occupant.execution_id == agent.execution_id:
                self.controller.require()
                self.store.put_agent(self.slug, replace(occupant, state=AWAITING))
            return self._record(Decision(agent.execution_id, agent.generation, reason, PENDING))
        replacement = self._replace(agent)
        return self._record(Decision(agent.execution_id, agent.generation, reason, RESUME, checkpoint, replacement))

    def _checkpoint(self, execution_id: str) -> str:
        usable = [
            entry
            for entry in self.checkpoints.list(execution_id)
            if entry.get("status") == "complete" and entry.get("compatibility") == self.compatibility
        ]
        return max(usable, key=lambda entry: entry["sequence"])["checkpoint_id"] if usable else ""

    def _replace(self, agent: AgentRecord) -> str:
        occupant = self.store.execution_occupants(self.slug)[agent.seat]
        if occupant.execution_id != agent.execution_id:
            if occupant.generation != agent.generation + 1:
                raise SwarmError("the seat moved past the fenced attempt")
            return occupant.execution_id
        self.controller.require()
        self.store.put_agent(self.slug, replace(occupant, state=FENCED))
        target = {"pod_namespace": agent.runtime_target["pod_namespace"], "pod_name": PENDING_POD}
        record = replace(
            agent, execution_id="", generation=0, state="working", conversation_id="", runtime_target=target
        )
        return self.controller.admit(record, agent.execution_id).execution_id

    def _record(self, decision: Decision) -> Decision:
        self._write(RECOVERIES, decision.execution_id, asdict(decision))
        return decision

    def _write(self, name: str, execution_id: str, value: dict) -> None:
        self.controller.require()
        self.store.redis.hset(self._key(name), execution_id, json.dumps(value))
