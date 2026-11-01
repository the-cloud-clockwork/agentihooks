from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from scripts.tick.deadline import Deadline
from scripts.tick.ports.source_control import (
    Capabilities,
    ChangeRef,
    ChangeState,
    CheckState,
    Comment,
    Merged,
    QueueEntry,
    QueueHealth,
    Run,
    Tag,
)

if TYPE_CHECKING:
    from scripts.swarm.store import SwarmConfig


@dataclass(frozen=True)
class Unsupported:
    operation: str


class UndeclaredEffect(PermissionError):
    pass


@runtime_checkable
class Ledger(Protocol):
    def export(self, *, deadline: Deadline) -> object: ...

    def mutate(self, operations: Sequence[Mapping[str, object]], *, deadline: Deadline) -> object: ...


@runtime_checkable
class Coordination(Protocol):
    def read(self, *, deadline: Deadline) -> object: ...

    def lease(self, *, deadline: Deadline) -> object: ...

    def lock(self, name: str, *, deadline: Deadline) -> object: ...

    def configure(self, settings: "SwarmConfig", *, deadline: Deadline) -> object: ...

    def capacity(self, *, deadline: Deadline) -> object: ...

    def mark(self, name: str, value: object, *, deadline: Deadline) -> object: ...


@runtime_checkable
class SourceControl(Protocol):
    def change(self, reference: ChangeRef, *, deadline: Deadline) -> ChangeState: ...

    def checks(self, reference: ChangeRef, *, deadline: Deadline) -> tuple[CheckState, ...]: ...

    def runs(self, reference: ChangeRef, *, deadline: Deadline) -> tuple[Run, ...]: ...

    def enqueue(self, reference: ChangeRef, *, deadline: Deadline) -> QueueEntry | Unsupported: ...

    def merge(self, reference: ChangeRef, *, deadline: Deadline) -> Merged: ...

    def queue_health(self, *, deadline: Deadline) -> QueueHealth | Unsupported: ...

    def tags(self, *, deadline: Deadline) -> tuple[Tag, ...]: ...

    def comment(self, reference: ChangeRef, body: str, *, deadline: Deadline) -> Comment: ...

    def capabilities(self, *, deadline: Deadline) -> Capabilities: ...


@runtime_checkable
class Runtime(Protocol):
    def inspect(self, handle: str, *, deadline: Deadline) -> object: ...

    def launch(self, request: Mapping[str, object], *, deadline: Deadline) -> object: ...

    def stop(self, handle: str, *, deadline: Deadline) -> object: ...

    def panes(self, *, deadline: Deadline) -> object: ...


@runtime_checkable
class Processes(Protocol):
    def scan(self, *, deadline: Deadline) -> object: ...


@runtime_checkable
class Inbox(Protocol):
    def read(self, address: str, *, deadline: Deadline) -> object: ...

    def send(self, address: str, body: str, *, deadline: Deadline) -> object: ...

    def close(self, item_id: str, *, deadline: Deadline) -> object: ...


@runtime_checkable
class Llm(Protocol):
    def classify(self, request: Mapping[str, object], *, deadline: Deadline) -> object: ...


@runtime_checkable
class Metrics(Protocol):
    def emit(self, measurements: Mapping[str, object], *, deadline: Deadline) -> object: ...


@runtime_checkable
class Host(Protocol):
    def read(self, name: str, *, deadline: Deadline) -> object: ...

    def run(self, request: Mapping[str, object], *, deadline: Deadline) -> object: ...
