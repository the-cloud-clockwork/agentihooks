import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from typing import Any, Iterable, Protocol
from uuid import NAMESPACE_URL, uuid5

from redis.exceptions import WatchError

WRITE_ATTEMPTS = 5
ACTIONS = frozenset(("spawn", "command", "drain", "terminate", "recover"))


class OperationConflict(ValueError):
    pass


class Phase(StrEnum):
    ACCEPTED = "accepted"
    ABSENT = "absent"
    UNKNOWN = "unknown"
    APPLIED = "applied"
    REFUSED = "refused"


@dataclass(frozen=True)
class OperationRequest:
    execution_id: str
    generation: int
    action: str
    payload: dict
    key: str


@dataclass(frozen=True)
class Operation:
    operation_id: str
    execution_id: str
    generation: int
    action: str
    backend: str
    payload_digest: str
    target: dict
    phase: Phase = Phase.ACCEPTED
    result: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Observation:
    phase: Phase
    execution_id: str = ""
    generation: int = 0
    backend: str = ""
    payload_digest: str = ""
    result: dict = field(default_factory=dict)


class OperationTransport(Protocol):
    backend: str

    def observe_operation(self, operation: Operation) -> Observation:
        """ABSENT proves no effect or in-flight request; UNKNOWN cannot authorize replay."""
        ...

    def apply_operation(self, operation: Operation, payload: dict) -> Observation:
        """Atomically deduplicate operation and execution identity and fence its generation."""
        ...


def digest(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _decode(raw: str) -> Operation:
    values = json.loads(raw)
    values["phase"] = Phase(values["phase"])
    return Operation(**values)


class OperationJournal:
    def __init__(self, store: Any):
        self.store = store

    def get(self, slug: str, operation_id: str) -> Operation | None:
        raw = self.store.redis.hget(self.store.key(slug, "runtime-operations"), operation_id)
        return _decode(raw) if raw else None

    def records(self, slug: str) -> list[Operation]:
        return [_decode(raw) for raw in self.store.redis.hvals(self.store.key(slug, "runtime-operations"))]

    def prepare(self, slug: str, request: OperationRequest) -> Operation:
        if not request.execution_id or not request.key or request.action not in ACTIONS:
            raise OperationConflict("invalid operation identity or action")
        if type(request.generation) is not int or request.generation < 1:
            raise OperationConflict("invalid execution generation")
        key = "spawn" if request.action == "spawn" else request.key
        operation_id = f"op-{uuid5(NAMESPACE_URL, json.dumps([slug, request.execution_id, key])).hex}"
        payload_digest = digest({"action": request.action, "payload": request.payload})

        def create(pipe):
            agent = self._current(slug, request.execution_id, request.generation, pipe)
            expected = Operation(
                operation_id,
                agent.execution_id,
                agent.generation,
                request.action,
                agent.runtime_backend,
                payload_digest,
                agent.runtime_target,
            )
            raw = pipe.hget(self.store.key(slug, "runtime-operations"), operation_id)
            if not raw:
                return expected
            previous = _decode(raw)
            if replace(previous, phase=Phase.ACCEPTED, result={}) != expected:
                raise OperationConflict("operation identity conflicts with payload or execution scope")
            return previous

        return self._write(slug, create)

    def settle(self, slug: str, operation: Operation, observation: Observation) -> Operation:
        def update(pipe):
            agent = self._current(slug, operation.execution_id, operation.generation, pipe)
            if agent.runtime_backend != operation.backend or agent.runtime_target != operation.target:
                raise OperationConflict("operation target changed")
            raw = pipe.hget(self.store.key(slug, "runtime-operations"), operation.operation_id)
            if not raw:
                raise OperationConflict("operation journal entry is missing")
            current = _decode(raw)
            if replace(current, phase=Phase.ACCEPTED, result={}) != replace(operation, phase=Phase.ACCEPTED, result={}):
                raise OperationConflict("operation identity changed")
            if current.phase is Phase.APPLIED:
                return current
            phase = Phase.ACCEPTED if observation.phase is Phase.ABSENT else observation.phase
            return replace(current, phase=phase, result=observation.result if phase is Phase.APPLIED else {})

        return self._write(slug, update)

    def _current(self, slug: str, execution_id: str, generation: int, pipe: Any) -> Any:
        raw = pipe.hget(self.store.key(slug, "executions"), execution_id)
        if not raw:
            raise OperationConflict("execution is not admitted")
        agent = json.loads(raw)
        occupant = self.store.execution_registry.occupants(slug, pipe).get(agent["seat"])
        if not occupant or (occupant.execution_id, occupant.generation) != (execution_id, generation):
            raise OperationConflict("execution generation is stale")
        return occupant

    def _write(self, slug: str, change: Any) -> Operation:
        key = self.store.key(slug, "runtime-operations")
        for _ in range(WRITE_ATTEMPTS):
            with self.store.redis.pipeline() as pipe:
                try:
                    pipe.watch(key, self.store.key(slug, "executions"))
                    operation = change(pipe)
                    pipe.multi()
                    pipe.hset(key, operation.operation_id, json.dumps(asdict(operation), sort_keys=True))
                    pipe.execute()
                    return operation
                except WatchError:
                    continue
        raise OperationConflict("operation journal kept changing; no outcome committed")


class Operations:
    def __init__(self, store: Any, transports: Iterable[OperationTransport], dispatch_enabled: bool = True):
        self.journal = store.operation_journal
        self.transports = {transport.backend: transport for transport in transports}
        self.dispatch_enabled = dispatch_enabled

    def execute(self, slug: str, request: OperationRequest) -> Operation:
        request = replace(request, payload=json.loads(json.dumps(request.payload, allow_nan=False)))
        operation = self.journal.prepare(slug, request)
        if operation.phase is Phase.APPLIED:
            return operation
        transport = self.transports.get(operation.backend)
        if transport is None:
            return self.journal.settle(slug, operation, Observation(Phase.UNKNOWN))
        observed = self._observe(transport, operation)
        if observed.phase is not Phase.ABSENT or not self.dispatch_enabled:
            return self.journal.settle(slug, operation, observed)
        operation = self.journal.settle(slug, operation, Observation(Phase.UNKNOWN))
        try:
            observed = transport.apply_operation(operation, request.payload)
        except (TimeoutError, ConnectionError):
            observed = Observation(Phase.UNKNOWN)
        return self.journal.settle(slug, operation, self._qualified(operation, observed))

    def unresolved(self, slug: str) -> list[Operation]:
        return [operation for operation in self.journal.records(slug) if operation.phase is not Phase.APPLIED]

    def recover(self, slug: str) -> list[Operation]:
        recovered = []
        for operation in self.unresolved(slug):
            transport = self.transports.get(operation.backend)
            observed = self._observe(transport, operation) if transport else Observation(Phase.UNKNOWN)
            recovered.append(self.journal.settle(slug, operation, observed))
        return recovered

    def runtime_ambiguous_operations(self, slug: str) -> int:
        return sum(operation.phase is Phase.UNKNOWN for operation in self.journal.records(slug))

    def _observe(self, transport: OperationTransport, operation: Operation) -> Observation:
        try:
            return self._qualified(operation, transport.observe_operation(operation))
        except (TimeoutError, ConnectionError):
            return Observation(Phase.UNKNOWN)

    def _qualified(self, operation: Operation, observation: Observation) -> Observation:
        if observation.phase is Phase.APPLIED and (
            observation.execution_id,
            observation.generation,
            observation.backend,
            observation.payload_digest,
        ) != (operation.execution_id, operation.generation, operation.backend, operation.payload_digest):
            return Observation(Phase.REFUSED)
        return observation
