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

    def recover(self, agent: AgentRecord, mode: Recovery, config: Any = None, text: str = "") -> Outcome: ...


def foreign(runtime: Runtime, operation: str, agent: AgentRecord) -> Outcome | None:
    if agent.runtime_backend == runtime.backend:
        return None
    return Outcome(
        operation, Status.REFUSED, runtime.backend, detail=f"runtime object belongs to {agent.runtime_backend}"
    )


class RuntimeRouter:
    def __init__(self, runtimes: Iterable[Runtime], default: str = LOCAL, disabled: Iterable[str] = ()):
        self.runtimes = {runtime.backend: runtime for runtime in runtimes}
        self.default, self.disabled = default, frozenset(disabled)
        self.failures = Counter()

    @classmethod
    def from_environ(cls, runtimes: Iterable[Runtime], environ: Mapping[str, str]) -> "RuntimeRouter":
        disabled = [name.strip() for name in environ.get(DISABLED_VARIABLE, "").split(",") if name.strip()]
        return cls(runtimes, environ.get(BACKEND_VARIABLE) or LOCAL, disabled)

    def spawn_backend(self) -> str:
        return LOCAL if self.default in self.disabled else self.default

    def spawn(self, request: SpawnRequest, needs: Iterable[Capability] = ()) -> Outcome:
        backend = self.spawn_backend()
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
        return self._own(agent, "terminate", Capability.TERMINATE, agent, homes)

    def recover(
        self, agent: AgentRecord, mode: Recovery = Recovery.REATTACH, config: Any = None, text: str = ""
    ) -> Outcome:
        need = Capability.NATIVE_RESUME if mode is Recovery.RESUME else Capability.RECOVER
        return self._own(agent, "recover", need, agent, mode, config, text)

    def capability_failures_total(self) -> int:
        return sum(self.failures.values())

    def _own(self, agent: AgentRecord, operation: str, need: Capability, *args) -> Outcome:
        backend = agent.runtime_backend
        if backend not in self.runtimes:
            return Outcome(operation, Status.UNAVAILABLE, backend, detail=f"no runtime registered for {backend}")
        if backend in self.disabled:
            return Outcome(operation, Status.UNAVAILABLE, backend, detail=f"runtime {backend} is disabled")
        return self._call(backend, operation, (need,), *args)

    def _call(self, backend: str, operation: str, needs: tuple, *args) -> Outcome:
        runtime = self.runtimes[backend]
        missing = [need for need in needs if need not in runtime.capabilities]
        if missing:
            self.failures[(backend, operation)] += 1
            return Outcome(operation, Status.UNSUPPORTED, backend, detail=f"{backend} lacks {', '.join(missing)}")
        return getattr(runtime, operation)(*args)
