"""The runtime protocol task orchestration calls: six operations, typed outcomes, capability flags checked first."""

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from scripts.swarm.store import AgentRecord

LOCAL = "local"
BACKEND_VARIABLE = "AGENTIHOOKS_RUNTIME_BACKEND"
DISABLED_VARIABLE = "AGENTIHOOKS_RUNTIME_DISABLED"


class Capability(StrEnum):
    SPAWN = "spawn"
    OBSERVE = "observe"
    COMMAND = "command"
    DRAIN = "drain"
    TERMINATE = "terminate"
    RECOVER = "recover"
    NATIVE_RESUME = "native_resume"


class Status(StrEnum):
    OK = "ok"
    AMBIGUOUS = "ambiguous"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"
    REFUSED = "refused"


class Unqualified(StrEnum):
    NO_EXECUTION = "no execution identity"
    NO_PROCESS = "runtime target names no process"
    NO_NAMESPACE = "local process namespace is unreadable"
    FOREIGN_NAMESPACE = "process belongs to another PID namespace"


class Recovery(StrEnum):
    REATTACH = "reattach"
    RESUME = "resume"


@dataclass(frozen=True)
class SpawnRequest:
    config: Any
    lane: str
    name: str
    task: dict


@dataclass(frozen=True)
class Placement:
    backend: str
    lanes: frozenset[str] = frozenset(("eng", "ci"))
    local_profiles: frozenset[str] = frozenset(("frontend",))

    def backend_for(self, request: SpawnRequest) -> str:
        placed = request.lane in self.lanes and request.task.get("profile") not in self.local_profiles
        return self.backend if placed else LOCAL


@dataclass(frozen=True)
class Outcome:
    operation: str
    status: Status
    backend: str
    value: Any = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status is Status.OK


class Runtime(Protocol):
    backend: str
    capabilities: frozenset[Capability]

    def spawn(self, request: SpawnRequest) -> Outcome: ...

    def observe(self, agent: AgentRecord) -> Outcome: ...

    def command(self, agent: AgentRecord, text: str) -> Outcome: ...

    def drain(self, agent: AgentRecord) -> Outcome: ...

    def terminate(self, agent: AgentRecord, homes: tuple = ()) -> Outcome: ...

    def recover(self, agent: AgentRecord, mode: Recovery, config: Any, text: str) -> Outcome: ...


def foreign(runtime: Runtime, operation: str, agent: AgentRecord) -> Outcome | None:
    if agent.runtime_backend == runtime.backend:
        return None
    return Outcome(
        operation, Status.REFUSED, runtime.backend, detail=f"runtime object belongs to {agent.runtime_backend}"
    )


def legacy(agent: AgentRecord) -> bool:
    return (
        agent.runtime_backend == LOCAL and not agent.execution_id and not agent.generation and not agent.runtime_target
    )


class RuntimeRouter:
    def __init__(
        self,
        runtimes: Iterable[Runtime],
        default: str = LOCAL,
        disabled: Iterable[str] = (),
        placement: Placement | None = None,
    ):
        self.runtimes = {runtime.backend: runtime for runtime in runtimes}
        self.default, self.disabled, self.placement = default, frozenset(disabled), placement
        self.failures, self.rejected = Counter(), Counter()

    @classmethod
    def from_environ(
        cls, runtimes: Iterable[Runtime], environ: Mapping[str, str], placement: Placement | None = None
    ) -> "RuntimeRouter":
        disabled = [name.strip() for name in environ.get(DISABLED_VARIABLE, "").split(",") if name.strip()]
        return cls(runtimes, environ.get(BACKEND_VARIABLE) or LOCAL, disabled, placement)

    def placed_backend(self, request: SpawnRequest | None = None) -> str:
        if self.placement is not None and request is not None:
            return self.placement.backend_for(request)
        return self.default

    def spawn_backend(self, request: SpawnRequest | None = None) -> str:
        wanted = self.placed_backend(request)
        return LOCAL if wanted in self.disabled else wanted

    def spawn(self, request: SpawnRequest, needs: Iterable[Capability] = ()) -> Outcome:
        backend = self.spawn_backend(request)
        if backend not in self.runtimes or backend in self.disabled:
            return Outcome("spawn", Status.UNAVAILABLE, backend, detail=f"no enabled runtime for {backend}")
        return self._call(backend, "spawn", (Capability.SPAWN, *needs), request)

    def observe(self, agent: AgentRecord) -> Outcome:
        return self._own(agent, "observe", Capability.OBSERVE, agent)

    def command(self, agent: AgentRecord, text: str) -> Outcome:
        return self._own(agent, "command", Capability.COMMAND, agent, text)

    def drain(self, agent: AgentRecord) -> Outcome:
        return self._own(agent, "drain", Capability.DRAIN, agent)

    def terminate(self, agent: AgentRecord, homes: tuple = ()) -> Outcome:
        backend, reason = agent.runtime_backend, Unqualified.NO_EXECUTION
        outcome = self._unavailable(agent, "terminate")
        if outcome is None and not agent.execution_id and not legacy(agent):
            outcome = Outcome("terminate", Status.REFUSED, backend, reason, reason.value)
        outcome = outcome or self._call(backend, "terminate", (Capability.TERMINATE,), agent, homes)
        if isinstance(outcome.value, Unqualified):
            self.rejected[(backend, outcome.value)] += 1
        return outcome

    def recover(self, agent: AgentRecord, mode: Recovery, config: Any = None, text: str = "") -> Outcome:
        need = Capability.NATIVE_RESUME if mode is Recovery.RESUME else Capability.RECOVER
        return self._own(agent, "recover", need, agent, mode, config, text)

    def capability_failures_total(self) -> int:
        return sum(self.failures.values())

    def unqualified_process_actions_rejected_total(self) -> int:
        return sum(self.rejected.values())

    def _own(self, agent: AgentRecord, operation: str, need: Capability, *args) -> Outcome:
        return self._unavailable(agent, operation) or self._call(agent.runtime_backend, operation, (need,), *args)

    def _unavailable(self, agent: AgentRecord, operation: str) -> Outcome | None:
        backend = agent.runtime_backend
        if backend not in self.runtimes:
            return Outcome(operation, Status.UNAVAILABLE, backend, detail=f"no runtime registered for {backend}")
        if backend in self.disabled:
            return Outcome(operation, Status.UNAVAILABLE, backend, detail=f"runtime {backend} is disabled")
        return None

    def _call(self, backend: str, operation: str, needs: tuple, *args) -> Outcome:
        runtime = self.runtimes[backend]
        missing = [need for need in needs if need not in runtime.capabilities]
        if missing:
            self.failures[(backend, operation)] += 1
            return Outcome(operation, Status.UNSUPPORTED, backend, detail=f"{backend} lacks {', '.join(missing)}")
        return getattr(runtime, operation)(*args)
