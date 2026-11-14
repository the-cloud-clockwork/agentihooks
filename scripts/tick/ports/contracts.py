from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from scripts.tick.deadline import Deadline
from scripts.tick.ports.source_control import (
    Capabilities,
    ChangeRef,
    ChangeState,
    CheckState,
    Comment,
    Merged,
    OpenChange,
    QueueEntry,
    QueueHealth,
    RateLimit,
    RepositoryRun,
    Run,
    Tag,
    WorkflowRun,
)

if TYPE_CHECKING:
    from scripts.swarm.store import AgentRecord, SwarmConfig


@dataclass(frozen=True)
class Unsupported:
    operation: str


class UndeclaredEffect(PermissionError):
    pass


@runtime_checkable
class Ledger(Protocol):
    def export(self, *, deadline: Deadline) -> object: ...

    def mutate(self, operations: Sequence[Mapping[str, object]], *, deadline: Deadline) -> object: ...

    def update_task(
        self,
        task_id: str,
        fields: Mapping[str, object],
        *,
        if_state: Sequence[str] = (),
        if_match: Mapping[str, object] | None = None,
        deadline: Deadline,
    ) -> dict[str, Any]: ...

    def task(self, task_id: str, *, deadline: Deadline) -> dict[str, Any]: ...

    def watchdog_copy(self, *, deadline: Deadline) -> dict[str, Any]: ...

    def notify(self, text: str, *, deadline: Deadline) -> None: ...

    def question(self, text: str, by: str, op_id: str, *, deadline: Deadline) -> None: ...

    def comment(self, task_id: str, text: str, by: str, *, deadline: Deadline) -> None: ...


STORE_READS = frozenset(
    {
        "agents",
        "claim_generation",
        "claims",
        "config",
        "earlier_lives",
        "execution",
        "execution_occupants",
        "failed_harness",
        "handoff",
        "handoff_envelope",
        "handoff_seat",
        "launches",
        "slugs",
        "culture.get",
        "execution_registry.agents",
        "execution_registry.get",
        "execution_registry.occupants",
        "execution_registry.records",
        "memory.learned",
        "memory.recaps",
        "seats.key",
        "seats.occupant",
        "seats.seat_of",
        *(
            f"redis.{command}"
            for command in (
                "exists",
                "get",
                "hexists",
                "hget",
                "hgetall",
                "hmget",
                "llen",
                "lrange",
                "mget",
                "pttl",
                "scard",
                "sismember",
                "smembers",
                "ttl",
                "xlen",
                "zcount",
                "zrange",
                "zscore",
            )
        ),
    }
)


@runtime_checkable
class Coordination(Protocol):
    def read(self, *, deadline: Deadline) -> object: ...

    def lease(self, *, deadline: Deadline) -> object: ...

    def lock(self, name: str, *, deadline: Deadline) -> object: ...

    def tick_lock(self, held: object, ttl_ms: int, caller: str, *, deadline: Deadline) -> str | None: ...

    def release_tick_lock(self, token: str, caller: str, *, deadline: Deadline) -> None: ...

    def configure(self, settings: "SwarmConfig", *, deadline: Deadline) -> object: ...

    def capacity(self, *, deadline: Deadline) -> object: ...

    def mark(self, name: str, value: object, *, deadline: Deadline) -> object: ...

    def marked(self, name: str, *, deadline: Deadline) -> str | None: ...

    def store_read(
        self, operation: str, args: Sequence[object], kwargs: Mapping[str, object], *, deadline: Deadline
    ) -> Any: ...

    def store_write(
        self, operation: str, args: Sequence[object], kwargs: Mapping[str, object], *, deadline: Deadline
    ) -> Any: ...


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

    def issue_state(self, reference: ChangeRef, *, deadline: Deadline) -> str: ...

    def close_issue(self, reference: ChangeRef, *, deadline: Deadline) -> str: ...

    def rate_limit(self, *, deadline: Deadline) -> RateLimit: ...

    def issues_enabled(self, *, deadline: Deadline) -> bool: ...

    def latest_runs(self, *, deadline: Deadline) -> tuple[RepositoryRun, ...]: ...

    def cancel_run(self, run_id: int, *, deadline: Deadline) -> None: ...

    def merged_at(self, commit: str, *, deadline: Deadline) -> str | None: ...

    def open_changes(self, *, deadline: Deadline) -> tuple[OpenChange, ...]: ...

    def workflow_runs(
        self, workflow: str, event: str, conclusion: str, since: str, *, deadline: Deadline
    ) -> tuple[WorkflowRun, ...]: ...

    def download(self, path: str, *, deadline: Deadline) -> bytes: ...

    def graph(self, document: str, variables: Mapping[str, str], *, deadline: Deadline) -> Any: ...


@runtime_checkable
class Runtime(Protocol):
    def inspect(self, handle: str, *, deadline: Deadline) -> object: ...

    def launch(self, request: Mapping[str, object], *, deadline: Deadline) -> object: ...

    def stop(self, handle: str, *, deadline: Deadline) -> object: ...

    def panes(self, *, deadline: Deadline) -> object: ...

    def live_names(self, *, deadline: Deadline) -> set[str]: ...

    def observe(self, agent: "AgentRecord", *, deadline: Deadline) -> Any: ...

    def retire(self, agent: "AgentRecord", *, homes: Sequence[Path] = (), deadline: Deadline) -> bool: ...

    def name_pane(self, agent: "AgentRecord", *, deadline: Deadline) -> None: ...

    def refusal(self, agent: "AgentRecord", *, deadline: Deadline) -> str: ...

    def resume(self, config: "SwarmConfig", agent: "AgentRecord", text: str, *, deadline: Deadline) -> Any: ...

    def dismiss(self, agent: "AgentRecord", *, deadline: Deadline) -> bool: ...

    def fence(self, held: object, deployment: str, *, deadline: Deadline) -> Any: ...


@runtime_checkable
class Processes(Protocol):
    def scan(self, *, deadline: Deadline) -> object: ...

    def rescan(self, *, deadline: Deadline) -> object: ...


@runtime_checkable
class Inbox(Protocol):
    def read(self, address: str, *, deadline: Deadline) -> object: ...

    def send(self, address: str, body: str, *, deadline: Deadline) -> object: ...

    def close(self, item_id: str, outcome: str, detail: str, *, deadline: Deadline) -> object: ...

    def settle_exit(self, name: str, seat: str, exit_text: str, *, deadline: Deadline) -> object: ...


@runtime_checkable
class Llm(Protocol):
    def classify(self, request: Mapping[str, object], *, deadline: Deadline) -> object: ...


@runtime_checkable
class Metrics(Protocol):
    def emit(self, measurements: Mapping[str, object], *, deadline: Deadline) -> object: ...

    def log(self, slug: str, rows: Sequence[object], *, deadline: Deadline) -> object: ...


@runtime_checkable
class Host(Protocol):
    def read(self, name: str, *, deadline: Deadline) -> object: ...

    def run(self, request: Mapping[str, object], *, deadline: Deadline) -> object: ...
