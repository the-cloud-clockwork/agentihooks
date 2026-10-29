import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from typing import Any, Iterable, Mapping, Protocol
from uuid import NAMESPACE_URL, uuid5

WRITE_ATTEMPTS = 5
ACTIONS = frozenset(("spawn", "command", "drain", "terminate", "recover"))
_CANONICAL = json.JSONEncoder(sort_keys=True, separators=(",", ":"), allow_nan=False)


SPAWN = "spawn"
_ERRORS = {
    "invalid": "invalid operation identity or action",
    "generation": "invalid execution generation",
    "payload": "operation identity conflicts with payload or execution scope",
    "target": "operation target changed",
    "missing": "operation journal entry is missing",
    "identity": "operation identity changed",
    "admission": "execution is not admitted",
    "stale": "execution generation is stale",
    "spec": "operation spec changed since it was journaled",
    "contention": "operation journal kept changing; no outcome committed",
}


class OperationConflict(ValueError):
    pass


class Phase(StrEnum):
    ACCEPTED = "accepted"
    ABSENT = "absent"
    UNKNOWN = "unknown"
    APPLIED = "applied"
    REFUSED = "refused"
    SUPERSEDED = "superseded"


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
    controller_epoch: int | None = None
    attempted: bool = False
    spec_digest: str = ""
    refusal: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Observation:
    phase: Phase
    execution_id: str = ""
    generation: int = 0
    backend: str = ""
    payload_digest: str = ""
    result: dict = field(default_factory=dict)
    refusal: dict = field(default_factory=dict, compare=False)


class OperationTransport(Protocol):
    backend: str

    def observe_operation(self, operation: Operation) -> Observation:
        """ABSENT proves no effect or in-flight request; UNKNOWN cannot authorize replay."""
        ...

    def apply_operation(self, operation: Operation, payload: dict) -> Observation:
        """Atomically deduplicate operation and execution identity and fence its generation."""
        ...


def digest(payload: dict) -> str:
    encoded = _CANONICAL.encode(payload).encode()
    return hashlib.sha256(encoded).hexdigest()


def _decode(row: dict) -> Operation:
    return Operation(**{**row, "phase": Phase(row["phase"])})


class OperationJournal:
    def __init__(self, store: Any):
        self.store = store

    def get(self, slug: str, operation_id: str) -> Operation | None:
        from scripts.swarm import fencing

        row = fencing.read(self.store, slug)["operations"].get(operation_id)
        return _decode(row) if row else None

    def records(self, slug: str) -> list[Operation]:
        from scripts.swarm import fencing

        return [_decode(row) for row in fencing.read(self.store, slug)["operations"].values()]

    def prepare(self, slug: str, request: OperationRequest, specs: Mapping[str, str] | None = None) -> Operation:
        if not request.execution_id or not request.key or request.action not in ACTIONS:
            raise OperationConflict(_ERRORS["invalid"])
        if type(request.generation) is not int or request.generation < 1:
            raise OperationConflict(_ERRORS["generation"])
        key = SPAWN if request.action == SPAWN else request.key
        operation_id = f"op-{uuid5(NAMESPACE_URL, json.dumps([slug, request.execution_id, key])).hex}"
        payload_digest = digest({"action": request.action, "payload": request.payload})

        def create(doc):
            from scripts.swarm import lease

            agent = self._current(request.execution_id, request.generation, doc)
            expected = Operation(
                operation_id,
                agent.execution_id,
                agent.generation,
                request.action,
                agent.runtime_backend,
                payload_digest,
                agent.runtime_target,
                controller_epoch=lease.EPOCH.get(),
                spec_digest=(specs or {}).get(agent.runtime_backend, ""),
            )
            raw = doc["operations"].get(operation_id)
            if not raw:
                return expected
            previous = _decode(raw)
            if replace(
                previous, phase=Phase.ACCEPTED, result={}, attempted=False, spec_digest="", refusal={}
            ) != replace(expected, controller_epoch=previous.controller_epoch, spec_digest=""):
                raise OperationConflict(_ERRORS["payload"])
            if agent.runtime_backend in (specs or {}) and previous.spec_digest not in ("", expected.spec_digest):
                raise OperationConflict(_ERRORS["spec"])
            return previous

        return self._write(slug, create)

    def settle(self, slug: str, operation: Operation, observation: Observation, attempted: bool = False) -> Operation:
        def update(doc):
            agent = self._current(operation.execution_id, operation.generation, doc)
            if agent.runtime_backend != operation.backend or agent.runtime_target != operation.target:
                raise OperationConflict(_ERRORS["target"])
            raw = doc["operations"].get(operation.operation_id)
            if not raw:
                raise OperationConflict(_ERRORS["missing"])
            current = _decode(raw)
            settled = {"phase": Phase.ACCEPTED, "result": {}, "attempted": False, "refusal": {}}
            if replace(current, **settled) != replace(operation, **settled):
                raise OperationConflict(_ERRORS["identity"])
            if current.phase in (Phase.APPLIED, Phase.SUPERSEDED):
                return current
            phase = Phase.ACCEPTED if observation.phase is Phase.ABSENT else observation.phase
            result = observation.result if phase is Phase.APPLIED else {}
            sent = (current.attempted or attempted) and phase in (Phase.UNKNOWN, Phase.APPLIED)
            refusal = {Phase.APPLIED: {}, Phase.REFUSED: observation.refusal}.get(phase, current.refusal)
            return replace(current, phase=phase, result=result, attempted=sent, refusal=refusal)

        return self._write(slug, update)

    def supersede_legacy_spawn(self, slug: str, operation: Operation) -> Operation:
        def update(doc):
            self._current(operation.execution_id, operation.generation, doc)
            raw = doc["operations"].get(operation.operation_id)
            if not raw or _decode(raw) != operation:
                raise OperationConflict(_ERRORS["identity"])
            if (
                operation.backend != "kubernetes"
                or operation.action != SPAWN
                or operation.spec_digest
                or operation.phase in (Phase.APPLIED, Phase.SUPERSEDED)
            ):
                return operation
            return replace(operation, phase=Phase.SUPERSEDED, result={"reason": "missing_spec_digest"})

        return self._write(slug, update)

    def _current(self, execution_id: str, generation: int, doc: dict) -> Any:
        from scripts.swarm.execution import occupants_of

        agent = doc["executions"].get(execution_id) or doc["archive"].get(execution_id)
        if not agent:
            raise OperationConflict(_ERRORS["admission"])
        occupant = occupants_of(doc).get(agent["seat"])
        if not occupant or (occupant.execution_id, occupant.generation) != (execution_id, generation):
            raise OperationConflict(_ERRORS["stale"])
        return occupant

    def _write(self, slug: str, change: Any) -> Operation:
        from scripts.swarm import fencing, lease
        from scripts.swarm.store import SwarmError

        def commit(doc):
            operation = change(doc)
            doc["operations"][operation.operation_id] = asdict(operation)
            return operation

        try:
            return lease.fenced(self.store, slug, lease.EPOCH.get(), commit)
        except SwarmError as error:
            if str(error) == fencing.CONTENTION:
                raise OperationConflict(_ERRORS["contention"]) from None
            raise


class Operations:
    def __init__(self, store: Any, transports: Iterable[OperationTransport], dispatch_enabled: bool = True):
        self.journal = store.operation_journal
        self.transports = {transport.backend: transport for transport in transports}
        self.dispatch_enabled = dispatch_enabled

    def execute(self, slug: str, request: OperationRequest) -> Operation:
        request = replace(request, payload=json.loads(json.dumps(request.payload)))
        operation = self.journal.prepare(slug, request, self._specs(request))
        if operation.phase in (Phase.APPLIED, Phase.SUPERSEDED):
            return operation
        transport = self.transports.get(operation.backend)
        if transport is None:
            return self.journal.settle(slug, operation, Observation(Phase.UNKNOWN))
        observed = self._observe(transport, operation)
        if observed.phase is not Phase.ABSENT or not self.dispatch_enabled:
            return self.journal.settle(slug, operation, observed)
        operation = self.journal.settle(slug, operation, Observation(Phase.UNKNOWN), attempted=True)
        try:
            observed = transport.apply_operation(operation, request.payload)
        except (TimeoutError, ConnectionError):
            observed = Observation(Phase.UNKNOWN)
        return self.journal.settle(slug, operation, self._qualified(operation, observed))

    def _specs(self, request: OperationRequest) -> dict[str, str]:
        if request.action != SPAWN:
            return {}
        return {
            backend: render(request.payload)
            for backend, transport in self.transports.items()
            if (render := getattr(transport, "spec_digest", None))
        }

    def unresolved(self, slug: str) -> list[Operation]:
        return [
            operation
            for operation in self.journal.records(slug)
            if operation.phase not in (Phase.APPLIED, Phase.SUPERSEDED)
        ]

    def recover(self, slug: str) -> list[Operation]:
        recovered = []
        for operation in self.unresolved(slug):
            transport = self.transports.get(operation.backend)
            observed = self._observe(transport, operation) if transport else Observation(Phase.UNKNOWN)
            try:
                result = self.journal.settle(slug, operation, observed)
            except OperationConflict:
                result = replace(operation, phase=Phase.REFUSED)
            recovered.append(result)
        return recovered

    def runtime_ambiguous_operations(self, slug: str) -> int:
        return sum(operation.phase is Phase.UNKNOWN for operation in self.journal.records(slug))

    def _observe(self, transport: OperationTransport, operation: Operation) -> Observation:
        try:
            return self._qualified(operation, transport.observe_operation(operation))
        except (TimeoutError, ConnectionError):
            return Observation(Phase.UNKNOWN)

    def _qualified(self, operation: Operation, observation: Observation) -> Observation:
        refused = Observation(Phase.REFUSED, refusal={"reason": "identity", "field": ""})
        if not isinstance(observation.phase, Phase):
            return refused
        if observation.phase is Phase.APPLIED and (
            type(observation.generation) is not int
            or (
                observation.execution_id,
                observation.generation,
                observation.backend,
                observation.payload_digest,
            )
            != (operation.execution_id, operation.generation, operation.backend, operation.payload_digest)
        ):
            return refused
        return observation
