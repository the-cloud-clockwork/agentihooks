from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime

from scripts.swarm import fencing, lease
from scripts.swarm.store import AgentRecord, RedisStore, SwarmError
from scripts.swarm_v2.auth_context import GrantRefused, Registration
from scripts.swarm_v2.controller import Controller


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


@dataclass(frozen=True)
class RenewalFence:
    key: str
    sequence: int
    digest: str


def _claim(doc: dict, task: str) -> TaskClaim | None:
    row = doc["claims"].get(task)
    return TaskClaim(**row) if row else None


class TaskAuthority:
    """The callback must validate a scoped credential on every call; labels confer no authority."""

    def __init__(self, store: RedisStore, controller: Controller, authorize: Callable[[str], Registration]) -> None:
        self.store, self.controller, self.authorize = store, controller, authorize
        self.slug = controller.slug

    def current(self, task: str) -> TaskClaim | None:
        return _claim(fencing.read(self.store, self.slug), task)

    def journal(self, task: str) -> list[dict]:
        return list(fencing.read(self.store, self.slug)["journals"].get(task, []))

    def replay(self, task: str) -> TaskClaim | None:
        def change(doc):
            self.controller.require()
            recovered = self._reconstruct(doc["journals"].get(task, []))
            current = _claim(doc, task)
            if current is not None and current != recovered:
                raise SwarmError("journal_conflict")
            if recovered is not None:
                self._commit(doc, recovered, current, "replayed")
            return recovered

        recovered = self._fenced(change)
        if recovered is not None:
            self._project(recovered)
        return recovered

    def _reconstruct(self, rows: list[dict]) -> TaskClaim | None:
        recovered = None
        for entry in rows:
            claim = TaskClaim(**entry["claim"])
            expected = (recovered.generation if recovered else 0) + (entry["event"] == "admitted")
            if claim.generation != expected:
                raise SwarmError("journal_conflict")
            recovered = claim
        return recovered

    def _execution(self, doc: dict, execution_id: str) -> AgentRecord:
        row = doc["executions"].get(execution_id) or doc["archive"].get(execution_id)
        if not row:
            self.store.execution_registry.conflict(
                self.slug, "unknown execution identity; display labels cannot identify attempts"
            )
        return AgentRecord(**row)

    def _scope(self, token: str, doc: dict | None = None) -> Registration:
        try:
            scope = self.authorize(token)
        except GrantRefused as error:
            if error.error_class == "stale_generation":
                self._stale()
            raise
        if not isinstance(scope, Registration) or scope.swarm_id != self.slug:
            raise SwarmError("forbidden_scope")
        doc = doc if doc is not None else fencing.read(self.store, self.slug)
        agent = self._execution(doc, scope.execution_id)
        if (agent.task, agent.seat, agent.generation) != (scope.task_id, scope.seat_id, scope.generation):
            raise SwarmError("forbidden_scope")
        current = self.store.execution_registry.occupants(self.slug, doc).get(scope.seat_id)
        if current is None or (current.execution_id, current.generation) != (scope.execution_id, scope.generation):
            self._stale()
        return scope

    def admit(self, token: str, lease_ms: int) -> TaskClaim:
        self._duration(lease_ms)

        def create(doc, scope, previous, projected):
            now = lease.now_ms(self.store)
            if previous and previous.state == "active" and previous.lease_deadline_ms > now:
                if previous.execution_id == scope.execution_id:
                    return previous
                raise SwarmError("claim_held")
            history = doc["journals"].get(scope.task_id, [])
            if history and previous is None:
                raise SwarmError("dependency_unavailable")
            if any(entry["claim"]["execution_id"] == scope.execution_id for entry in history):
                self._stale()
            agent = self._execution(doc, scope.execution_id)
            if previous is None and projected not in (None, agent.name):
                raise SwarmError("claim_held")
            if previous and previous.state == "completed":
                raise SwarmError("claim_held")
            return TaskClaim(
                scope.task_id,
                agent.name,
                previous.generation + 1 if previous else 1,
                scope.execution_id,
                scope.generation,
                now + lease_ms,
            )

        return self._write(token, "admitted", create)

    def renew(self, token: str, generation: int, lease_ms: int, fence: RenewalFence | None = None) -> TaskClaim:
        self._duration(lease_ms)

        def update(doc, scope, previous, projected):
            self._holder(scope, generation, previous)
            return replace(previous, lease_deadline_ms=lease.now_ms(self.store) + lease_ms)

        return self._write(token, "renewed", update, fence)

    def release(self, token: str, generation: int) -> TaskClaim:
        def update(doc, scope, previous, projected):
            self._holder(scope, generation, previous)
            return replace(previous, state="released")

        return self._write(token, "released", update)

    def complete(self, token: str, generation: int, result: dict) -> TaskClaim:
        def update(doc, scope, previous, projected):
            if previous and previous.state == "completed":
                self._identity(scope, generation, previous)
                if previous.result != result:
                    raise SwarmError("operation_conflict")
                return previous
            self._holder(scope, generation, previous)
            return replace(previous, state="completed", result=result)

        return self._write(token, "completed", update)

    def stale_generation_rejections_total(self) -> int:
        return fencing.count(self.store, self.slug, fencing.STALE_REJECTIONS)

    def _stale(self) -> None:
        fencing.bump(self.store, self.slug, fencing.STALE_REJECTIONS)
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

    def _fenced(self, change: Callable[[dict], object]):
        try:
            return lease.fenced(self.store, self.slug, self.controller.held, change)
        except SwarmError as error:
            if str(error) == fencing.CONTENTION:
                raise SwarmError("dependency_unavailable") from None
            raise

    def _write(
        self, token: str, event: str, action: Callable[..., TaskClaim], fence: RenewalFence | None = None
    ) -> TaskClaim:
        scope = self._scope(token)
        projected = self.store.claimant(self.slug, scope.task_id)

        def change(doc):
            self.controller.require()
            if self._scope(token, doc) != scope:
                raise SwarmError("forbidden_scope")
            previous = _claim(doc, scope.task_id)
            journal = doc["journals"].get(scope.task_id, [])
            if previous is not None and (not journal or previous != TaskClaim(**journal[-1]["claim"])):
                raise SwarmError("journal_conflict")
            current = action(doc, scope, previous, projected)
            if current == previous and fence is None:
                return current, False
            audit = doc["grants"][scope.grant_id]
            deadline = int(datetime.fromisoformat(audit["expires_at"]).timestamp() * 1000)
            self._commit(doc, current, previous, event, deadline, fence)
            return current, True

        current, written = self._fenced(change)
        if written:
            self._project(current)
        return current

    def _commit(
        self,
        doc: dict,
        current: TaskClaim,
        previous: TaskClaim | None,
        event: str,
        worker_deadline_ms: int | None = None,
        fence: RenewalFence | None = None,
    ) -> None:
        now = lease.now_ms(self.store)
        if worker_deadline_ms is not None and worker_deadline_ms <= now:
            raise SwarmError("worker credential has expired")
        if event == "admitted" and current.lease_deadline_ms <= now:
            self._stale()
        if event not in ("admitted", "replayed") and (previous.state != "active" or previous.lease_deadline_ms <= now):
            self._stale()
        if fence is not None:
            seen = doc["fences"].get(fence.key)
            if seen and (
                seen["sequence"] > fence.sequence
                or (seen["sequence"] == fence.sequence and seen["digest"] != fence.digest)
            ):
                raise SwarmError("out_of_order")
            doc["fences"][fence.key] = {"sequence": fence.sequence, "digest": fence.digest}
        task = current.task_id
        doc["claims"][task] = asdict(current)
        journal = doc["journals"].setdefault(task, [])
        if event == "admitted" and previous and previous.state == "active":
            journal.append({"event": "fenced", "claim": asdict(replace(previous, state="fenced"))})
        if event != "replayed":
            journal.append({"event": event, "claim": asdict(current)})

    def _project(self, current: TaskClaim) -> None:
        live = current.state == "active" and current.lease_deadline_ms > lease.now_ms(self.store)
        self.store.project_claim(
            self.slug, current.task_id, current.holder if live else None, current.lease_deadline_ms
        )
