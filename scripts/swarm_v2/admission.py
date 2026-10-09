"""Bound pending execution attempts by global and per swarm caps, free provider sessions and approved templates.

Admission never chooses a node or instance: an approved template only proves some permitted node can hold the request.
"""

import json
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from uuid import uuid4

from redis.exceptions import WatchError

from scripts.swarm.store import PREFIX, RedisStore, SwarmError

ADMITTED, REPLAYED, DEFERRED, IMPOSSIBLE = "admitted", "replayed", "deferred", "impossible"
EXPIRED, ACTIVATED, RELEASED = "expired", "activated", "released"
ATTEMPTS = 8
CONFLICT = "pending admission kept conflicting; retry on the next tick"


@dataclass(frozen=True)
class Resources:
    memory_mib: int
    cpu_millis: int

    def fits(self, capacity: "Resources") -> bool:
        return self.memory_mib <= capacity.memory_mib and self.cpu_millis <= capacity.cpu_millis


@dataclass(frozen=True)
class Template:
    name: str
    capacity: Resources


@dataclass(frozen=True)
class Policy:
    global_cap: int
    swarm_cap: int
    ttl_ms: int
    templates: tuple[Template, ...]
    default: Resources


@dataclass(frozen=True)
class Decision:
    task: str
    outcome: str
    reservation: str = ""
    deadline_ms: int = 0
    reason: str = ""


def requested(task: dict, default: Resources) -> Resources:
    asked = task.get("resources") or {}
    return Resources(int(asked.get("memory_mib", default.memory_mib)), int(asked.get("cpu_millis", default.cpu_millis)))


def impossible(task_id: str, need: Resources, templates: tuple[Template, ...]) -> str:
    if any(need.fits(t.capacity) for t in templates):
        return ""
    offered = "; ".join(f"{t.name} {t.capacity.memory_mib} MiB {t.capacity.cpu_millis} millicpu" for t in templates)
    return (
        f"task {task_id} requests {need.memory_mib} MiB memory and {need.cpu_millis} millicpu, more than every "
        f"approved template ({offered or 'none'}); lower the task resources or approve a larger template"
    )


def _held(row: dict, task: str) -> Decision:
    return Decision(task, REPLAYED, row["reservation"], row["deadline_ms"])


class PendingAdmission:
    """Pending reservations live apart from provider sessions; the slot source is read, never written."""

    global_key = f"{PREFIX}:pending-admission"

    def __init__(self, store: RedisStore, policy: Policy, provider_slots: Callable[[str], int]) -> None:
        self.store, self.policy, self.provider_slots = store, policy, provider_slots

    def key(self, slug: str) -> str:
        return self.store.key(slug, "pending-admission")

    def total_key(self, slug: str) -> str:
        return self.store.key(slug, "pending-admission-total")

    def admit(self, slug: str, tasks: Iterable[dict], now_ms: int) -> list[Decision]:
        tasks, slots = list(tasks), self.provider_slots(slug)
        return self._retry(lambda: self._admit(slug, tasks, slots, now_ms))

    def activate(self, slug: str, task: str, reservation: str) -> bool:
        return self._retry(lambda: self._end_once(slug, task, reservation, ACTIVATED))

    def release(self, slug: str, task: str, reservation: str) -> bool:
        return self._retry(lambda: self._end_once(slug, task, reservation, RELEASED))

    def pending(self, slug: str, now_ms: int) -> dict[str, Decision]:
        rows = {task: json.loads(raw) for task, raw in self.store.redis.hgetall(self.key(slug)).items()}
        return {task: _held(row, task) for task, row in rows.items() if row["deadline_ms"] > now_ms}

    def pending_execution_admission_total(self, slug: str) -> dict[str, int]:
        return {k: int(v) for k, v in self.store.redis.hgetall(self.total_key(slug)).items()}

    def _retry(self, attempt: Callable):
        for _ in range(ATTEMPTS):
            try:
                return attempt()
            except WatchError:
                continue
        raise SwarmError(CONFLICT)

    def _admit(self, slug: str, tasks: list[dict], slots: int, now_ms: int) -> list[Decision]:
        key = self.key(slug)
        with self.store.redis.pipeline() as pipe:
            pipe.watch(self.global_key, key)
            rows = {task: json.loads(raw) for task, raw in pipe.hgetall(key).items()}
            expired = [task for task, row in rows.items() if row["deadline_ms"] <= now_ms]
            live = {task: row for task, row in rows.items() if row["deadline_ms"] > now_ms}
            across = pipe.zcount(self.global_key, f"({now_ms}", "+inf")
            decisions, written = [], {}
            for task in tasks:
                decision = self._decide(task, live, written, (across, slots), now_ms)
                if decision.outcome == ADMITTED:
                    written[decision.task] = {
                        "reservation": decision.reservation,
                        "deadline_ms": decision.deadline_ms,
                        "resources": asdict(requested(task, self.policy.default)),
                    }
                decisions.append(decision)
            pipe.multi()
            pipe.zremrangebyscore(self.global_key, "-inf", now_ms)
            if expired:
                pipe.hdel(key, *expired)
            for task, row in written.items():
                pipe.hset(key, task, json.dumps(row))
                pipe.zadd(self.global_key, {f"{slug}\t{task}": row["deadline_ms"]})
            counts = Counter(d.outcome for d in decisions) + Counter({EXPIRED: len(expired)})
            for outcome, count in counts.items():
                pipe.hincrby(self.total_key(slug), outcome, count)
            pipe.execute()
        return decisions

    def _decide(self, task: dict, live: dict, written: dict, bounds: tuple[int, int], now_ms: int) -> Decision:
        task_id = task["id"]
        if task_id in live:
            return _held(live[task_id], task_id)
        if reason := impossible(task_id, requested(task, self.policy.default), self.policy.templates):
            return Decision(task_id, IMPOSSIBLE, reason=reason)
        if reason := self._full(len(live) + len(written), bounds[0] + len(written), bounds[1]):
            return Decision(task_id, DEFERRED, reason=reason)
        return Decision(task_id, ADMITTED, uuid4().hex, now_ms + self.policy.ttl_ms)

    def _full(self, mine: int, across: int, slots: int) -> str:
        cap = self.policy
        if cap.global_cap <= 0 or cap.swarm_cap <= 0:
            return "distributed admission is set to zero"
        if mine >= cap.swarm_cap or across >= cap.global_cap:
            return (
                f"pending cap reached: {mine} of {cap.swarm_cap} pending attempts for this swarm, "
                f"{across} of {cap.global_cap} across swarms"
            )
        if mine >= slots:
            return f"provider slots reached: {mine} pending attempts already wait on {slots} free provider sessions"
        return ""

    def _end_once(self, slug: str, task: str, reservation: str, outcome: str) -> bool:
        key = self.key(slug)
        with self.store.redis.pipeline() as pipe:
            pipe.watch(key)
            raw = pipe.hget(key, task)
            if raw is None or json.loads(raw)["reservation"] != reservation:
                return False
            pipe.multi()
            pipe.hdel(key, task)
            pipe.zrem(self.global_key, f"{slug}\t{task}")
            pipe.hincrby(self.total_key(slug), outcome, 1)
            pipe.execute()
        return True
