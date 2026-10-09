import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from redis.exceptions import WatchError

from scripts.swarm import lease
from scripts.swarm.store import RedisStore, SwarmError
from scripts.swarm_v2.auth_context import GrantRefused, Registration
from scripts.swarm_v2.controller import Controller

WRITE_ATTEMPTS = 5


@dataclass(frozen=True)
class TaskClaim:
    task_id: str
    holder: str
    generation: int
    execution_id: str
    execution_generation: int
    lease_deadline_ms: int
    state: str = "active"
    result: dict = field(default_factory=dict)


class TaskAuthority:
    """The callback must validate a scoped credential on every call; labels confer no authority."""

    def __init__(self, store: RedisStore, controller: Controller, authorize: Callable[[str], Registration]) -> None:
        self.store, self.controller, self.authorize = store, controller, authorize
        self.slug = controller.slug

    def current(self, task: str) -> TaskClaim | None:
        raw = self.store.redis.get(self.store.key(self.slug, "task-authority", task))
        return TaskClaim(**json.loads(raw)) if raw else None

    def journal(self, task: str) -> list[dict]:
        return [
            json.loads(raw) for raw in self.store.redis.lrange(self.store.key(self.slug, "claim-journal", task), 0, -1)
        ]

    def replay(self, task: str) -> TaskClaim | None:
        key = self.store.key(self.slug, "task-authority", task)
        journal = self.store.key(self.slug, "claim-journal", task)
        projection = self.store.key(self.slug, "claim", task)
        for _ in range(WRITE_ATTEMPTS):
            with self.store.redis.pipeline() as pipe:
                try:
                    pipe.watch(key, journal, projection, self.store.key(self.slug, "control-owner"))
                    self.controller.require()
                    rows = pipe.lrange(journal, 0, -1)
                    recovered = self._reconstruct(rows)
                    raw = pipe.get(key)
                    current = TaskClaim(**json.loads(raw)) if raw else None
                    if current is not None and current != recovered:
                        raise SwarmError("journal_conflict")
                    if recovered is None:
                        return None
                    pipe.multi()
                    pipe.set(key, json.dumps(asdict(recovered)))
                    self._project(pipe, projection, recovered)
                    pipe.execute()
                    return recovered
                except WatchError:
                    continue
        raise SwarmError("dependency_unavailable")

    def _reconstruct(self, rows: list[str]) -> TaskClaim | None:
        recovered = None
        for raw in rows:
            entry = json.loads(raw)
            claim = TaskClaim(**entry["claim"])
            expected = (recovered.generation if recovered else 0) + (entry["event"] == "admitted")
            if claim.generation != expected:
                raise SwarmError("journal_conflict")
            recovered = claim
        return recovered

    def _scope(self, token: str) -> Registration:
        try:
            scope = self.authorize(token)
        except GrantRefused as error:
            if error.error_class == "stale_generation":
                self._stale()
            raise
        if not isinstance(scope, Registration) or scope.swarm_id != self.slug:
            raise SwarmError("forbidden_scope")
        agent = self.store.execution(self.slug, scope.execution_id)
        if (agent.task, agent.seat, agent.generation) != (scope.task_id, scope.seat_id, scope.generation):
            raise SwarmError("forbidden_scope")
        current = self.store.execution_occupants(self.slug).get(scope.seat_id)
        if current is None or (current.execution_id, current.generation) != (scope.execution_id, scope.generation):
            self._stale()
        return scope

    def admit(self, token: str, lease_ms: int) -> TaskClaim:
        self._duration(lease_ms)

        def create(pipe, scope, previous):
            now = lease.now_ms(self.store)
            if previous and previous.state == "active" and previous.lease_deadline_ms > now:
                if previous.execution_id == scope.execution_id:
                    return previous
                raise SwarmError("claim_held")
            history = pipe.lrange(self.store.key(self.slug, "claim-journal", scope.task_id), 0, -1)
            if history and previous is None:
                raise SwarmError("dependency_unavailable")
            if any(json.loads(raw)["claim"]["execution_id"] == scope.execution_id for raw in history):
                self._stale()
            if previous is None and pipe.exists(self.store.key(self.slug, "claim", scope.task_id)):
                raise SwarmError("claim_held")
            if previous and previous.state == "completed":
                raise SwarmError("claim_held")
            agent = self.store.execution(self.slug, scope.execution_id)
            return TaskClaim(
                scope.task_id,
                agent.name,
                previous.generation + 1 if previous else 1,
                scope.execution_id,
                scope.generation,
                now + lease_ms,
            )

        return self._write(token, "admitted", create)

    def renew(self, token: str, generation: int, lease_ms: int) -> TaskClaim:
        self._duration(lease_ms)

        def update(pipe, scope, previous):
            self._holder(scope, generation, previous)
            return replace(previous, lease_deadline_ms=lease.now_ms(self.store) + lease_ms)

        return self._write(token, "renewed", update)

    def release(self, token: str, generation: int) -> TaskClaim:
        def update(pipe, scope, previous):
            self._holder(scope, generation, previous)
            return replace(previous, state="released")

        return self._write(token, "released", update)

    def complete(self, token: str, generation: int, result: dict) -> TaskClaim:
        def update(pipe, scope, previous):
            if previous and previous.state == "completed":
                self._identity(scope, generation, previous)
                if previous.result != result:
                    raise SwarmError("operation_conflict")
                return previous
            self._holder(scope, generation, previous)
            return replace(previous, state="completed", result=result)

        return self._write(token, "completed", update)

    def stale_generation_rejections_total(self) -> int:
        return int(self.store.redis.get(self.store.key(self.slug, "stale-generation-rejections")) or 0)

    def _stale(self) -> None:
        self.store.redis.incr(self.store.key(self.slug, "stale-generation-rejections"))
        raise SwarmError("stale_generation")

    def _duration(self, lease_ms: int) -> None:
        if type(lease_ms) is not int or lease_ms <= 0:
            raise SwarmError("invalid_request")

    def _identity(self, scope: Registration, generation: int, previous: TaskClaim | None) -> None:
        if type(generation) is not int or generation < 1:
            raise SwarmError("invalid_request")
        if previous is None or (previous.generation, previous.execution_id, previous.execution_generation) != (
            generation,
            scope.execution_id,
            scope.generation,
        ):
            self._stale()

    def _holder(self, scope: Registration, generation: int, previous: TaskClaim | None) -> None:
        self._identity(scope, generation, previous)
        if previous.state != "active" or previous.lease_deadline_ms <= lease.now_ms(self.store):
            self._stale()

    def _write(self, token: str, event: str, action: Callable[..., TaskClaim]) -> TaskClaim:
        scope = self._scope(token)
        key = self.store.key(self.slug, "task-authority", scope.task_id)
        journal = self.store.key(self.slug, "claim-journal", scope.task_id)
        projection = self.store.key(self.slug, "claim", scope.task_id)
        keys = [
            key,
            journal,
            projection,
            *[
                self.store.key(self.slug, part)
                for part in (
                    "executions",
                    "control-owner",
                    "launch-grants",
                    "launch-registrations",
                    "launch-grants-disabled",
                )
            ],
        ]
        for _ in range(WRITE_ATTEMPTS):
            with self.store.redis.pipeline() as pipe:
                try:
                    pipe.watch(*keys)
                    self.controller.require()
                    checked = self._scope(token)
                    if checked != scope:
                        raise SwarmError("forbidden_scope")
                    raw = pipe.get(key)
                    previous = TaskClaim(**json.loads(raw)) if raw else None
                    tail = pipe.lindex(journal, -1)
                    if previous is not None and (tail is None or previous != TaskClaim(**json.loads(tail)["claim"])):
                        raise SwarmError("journal_conflict")
                    current = action(pipe, scope, previous)
                    if current == previous:
                        return current
                    pipe.multi()
                    pipe.set(key, json.dumps(asdict(current)))
                    if event == "admitted" and previous and previous.state == "active":
                        pipe.rpush(
                            journal, json.dumps({"event": "fenced", "claim": asdict(replace(previous, state="fenced"))})
                        )
                    pipe.rpush(journal, json.dumps({"event": event, "claim": asdict(current)}))
                    self._project(pipe, projection, current)
                    pipe.execute()
                    return current
                except WatchError:
                    continue
        raise SwarmError("dependency_unavailable")

    def _project(self, pipe: Any, key: str, claim: TaskClaim) -> None:
        remaining = claim.lease_deadline_ms - lease.now_ms(self.store)
        if claim.state == "active" and remaining > 0:
            pipe.set(key, claim.holder, px=remaining)
        else:
            pipe.delete(key)
