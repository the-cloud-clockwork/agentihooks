from collections.abc import Callable
from dataclasses import dataclass, fields
from typing import cast

from scripts.tick.deadline import Deadline
from scripts.tick.ports.contracts import (
    Coordination,
    Host,
    Inbox,
    Ledger,
    Llm,
    Metrics,
    Processes,
    Runtime,
    SourceControl,
    UndeclaredEffect,
)

_OPERATIONS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "ledger": (frozenset(["export"]), frozenset(["mutate"])),
    "coordination": (frozenset(["read", "capacity"]), frozenset(["lease", "lock", "configure", "mark"])),
    "source_control": (
        frozenset(["change", "checks", "runs", "queue_health", "tags", "capabilities"]),
        frozenset(["enqueue", "merge", "comment"]),
    ),
    "runtime": (frozenset(["inspect", "panes"]), frozenset(["launch", "stop"])),
    "processes": (frozenset(["scan"]), frozenset()),
    "inbox": (frozenset(["read"]), frozenset(["send", "close"])),
    "llm": (frozenset(), frozenset(["classify"])),
    "metrics": (frozenset(), frozenset(["emit"])),
    "host": (frozenset(["read"]), frozenset(["run"])),
}


@dataclass(frozen=True)
class _RestrictedPort:
    adapter: object
    name: str
    allowed: bool

    def __getattr__(self, operation: str) -> Callable[..., object]:
        reads, writes = _OPERATIONS[self.name]
        if operation not in reads | writes:
            raise AttributeError(operation)

        def invoke(*args: object, **kwargs: object) -> object:
            if operation in writes and not self.allowed:
                raise UndeclaredEffect(f"{self.name} is not a declared effect")
            deadline = kwargs.get("deadline")
            if not isinstance(deadline, Deadline):
                raise TypeError("tools require a Deadline keyword argument")
            if deadline.expired:
                raise TimeoutError("tool deadline expired")
            method = cast(Callable[..., object], getattr(self.adapter, operation))
            return method(*args, **kwargs)

        return invoke


@dataclass(frozen=True)
class Tools:
    ledger: Ledger
    coordination: Coordination
    source_control: SourceControl
    runtime: Runtime
    processes: Processes
    inbox: Inbox
    llm: Llm
    metrics: Metrics
    host: Host

    def for_effects(self, effects: frozenset[str]) -> "Tools":
        ports = {
            field.name: _RestrictedPort(getattr(self, field.name), field.name, field.name in effects)
            for field in fields(self)
        }
        return Tools(
            ledger=cast(Ledger, ports["ledger"]),
            coordination=cast(Coordination, ports["coordination"]),
            source_control=cast(SourceControl, ports["source_control"]),
            runtime=cast(Runtime, ports["runtime"]),
            processes=cast(Processes, ports["processes"]),
            inbox=cast(Inbox, ports["inbox"]),
            llm=cast(Llm, ports["llm"]),
            metrics=cast(Metrics, ports["metrics"]),
            host=cast(Host, ports["host"]),
        )
