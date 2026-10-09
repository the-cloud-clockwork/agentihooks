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
COMMIT = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local leader_raw = redis.call('GET', KEYS[4])
if not leader_raw then return 'controller_stale' end
local leader = cjson.decode(leader_raw)
local expected = cjson.decode(ARGV[4])
if leader.owner ~= expected.owner or leader.epoch ~= expected.epoch or leader.expires_at <= now then
    return 'controller_stale'
end
if (redis.call('GET', KEYS[1]) or '') ~= ARGV[2] then return 'stale_generation' end
local current = cjson.decode(ARGV[1])
if ARGV[3] == 'admitted' and current.lease_deadline_ms <= now then return 'stale_generation' end
if ARGV[3] ~= 'admitted' and ARGV[3] ~= 'replayed' then
    local previous = cjson.decode(ARGV[2])
    if previous.state ~= 'active' or previous.lease_deadline_ms <= now then return 'stale_generation' end
end
redis.call('SET', KEYS[1], ARGV[1])
if ARGV[5] ~= '' then redis.call('RPUSH', KEYS[2], ARGV[5]) end
if ARGV[6] ~= '' then redis.call('RPUSH', KEYS[2], ARGV[6]) end
if current.state == 'active' and current.lease_deadline_ms > now then
    redis.call('SET', KEYS[3], current.holder, 'PXAT', current.lease_deadline_ms)
else
    redis.call('DEL', KEYS[3])
end
return 'committed'
"""


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
                    self._commit(pipe, recovered, current, "replayed")
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
                    self._commit(pipe, current, previous, event)
                    return current
                except WatchError:
                    continue
        raise SwarmError("dependency_unavailable")

    def _commit(self, pipe: Any, current: TaskClaim, previous: TaskClaim | None, event: str) -> None:
        task = current.task_id
        fenced = (
            replace(previous, state="fenced")
            if event == "admitted" and previous and previous.state == "active"
            else None
        )
        pipe.multi()
        pipe.eval(
            COMMIT,
            4,
            self.store.key(self.slug, "task-authority", task),
            self.store.key(self.slug, "claim-journal", task),
            self.store.key(self.slug, "claim", task),
            self.store.key(self.slug, "control-owner"),
            json.dumps(asdict(current)),
            json.dumps(asdict(previous)) if previous else "",
            event,
            json.dumps(asdict(self.controller.held)),
            json.dumps({"event": "fenced", "claim": asdict(fenced)}) if fenced else "",
            json.dumps({"event": event, "claim": asdict(current)}) if event != "replayed" else "",
        )
        result = pipe.execute()[0]
        if result == "stale_generation":
            self._stale()
        if result != "committed":
            raise SwarmError("the controller lease is stale")
