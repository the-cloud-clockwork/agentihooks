"""Bind tick tool ports to a step deadline for legacy reap helpers."""

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.swarm.store import PREFIX, AgentRecord, SwarmConfig
from scripts.tick.deadline import Deadline
from scripts.tick.ports import Coordination, Inbox, Ledger, Metrics, Processes, Runtime
from scripts.tick.ports.contracts import STORE_READS


@dataclass(frozen=True)
class RuntimeCalls:
    port: Runtime
    deadline: Deadline

    def live_names(self) -> set[str]:
        return self.port.live_names(deadline=self.deadline)

    def observe(self, agent: AgentRecord) -> Any:
        return self.port.observe(agent, deadline=self.deadline)

    def retire(self, agent: AgentRecord, homes: Sequence[Path] = ()) -> bool:
        return self.port.retire(agent, homes=homes, deadline=self.deadline)

    def name_pane(self, agent: AgentRecord) -> None:
        self.port.name_pane(agent, deadline=self.deadline)

    def refusal(self, agent: AgentRecord) -> str:
        return self.port.refusal(agent, deadline=self.deadline)

    def resume(self, config: SwarmConfig, agent: AgentRecord, text: str) -> Any:
        return self.port.resume(config, agent, text, deadline=self.deadline)

    def dismiss(self, agent: AgentRecord) -> bool:
        return self.port.dismiss(agent, deadline=self.deadline)


@dataclass(frozen=True)
class AdmissionCalls:
    """The pending admission gate of the spawn helpers, over the runtime port."""

    port: Runtime
    deadline: Deadline

    def admit(self, slug: str, tasks: Sequence[Mapping[str, Any]], now_ms: int) -> Any:
        return self.port.admit(slug, tasks, now_ms, deadline=self.deadline)

    def release(self, slug: str, task_id: str, reservation: str) -> None:
        self.port.release_admission(slug, task_id, reservation, deadline=self.deadline)

    def held(self, slug: str, task_id: str) -> str:
        return self.port.held_admission(slug, task_id, deadline=self.deadline)

    def activate(self, slug: str, task_id: str, reservation: str) -> bool:
        return self.port.activate_admission(slug, task_id, reservation, deadline=self.deadline)


_STORE_GROUPS = frozenset({"redis", "seats", "memory", "culture", "execution_registry", "names"})


def _store_operation(port: Coordination, deadline: Deadline, path: str) -> Callable[..., Any]:
    if path.rpartition(".")[2].startswith("_"):
        raise AttributeError(path)
    send = port.store_read if path in STORE_READS else port.store_write

    def operation(*args: Any, **kwargs: Any) -> Any:
        return send(path, args, kwargs, deadline=deadline)

    operation.__module__, operation.__qualname__ = "scripts.swarm.store", f"RedisStore.{path}"
    return operation


@dataclass(frozen=True)
class _StoreGroup:
    port: Coordination
    deadline: Deadline
    name: str

    def __getattr__(self, name: str) -> Callable[..., Any]:
        return _store_operation(self.port, self.deadline, f"{self.name}.{name}")


@dataclass(frozen=True)
class SpawnStoreCalls:
    """The swarm store the spawn helpers call, every read and write a coordination port operation bound to the deadline."""

    port: Coordination
    deadline: Deadline
    inbox: Inbox

    def key(self, slug: str, *parts: str) -> str:
        return ":".join((PREFIX, slug, *parts))

    def settle_exit(self, name: str, seat: str, exit_text: str) -> None:
        """Settle the leaving agent's inbox items through the inbox port."""
        self.inbox.settle_exit(name, seat, exit_text, deadline=self.deadline)

    def __getattr__(self, name: str) -> Any:
        if name in _STORE_GROUPS:
            return _StoreGroup(self.port, self.deadline, name)
        return _store_operation(self.port, self.deadline, name)


@dataclass(frozen=True)
class LedgerCalls:
    port: Ledger
    deadline: Deadline

    def update_task(
        self,
        slug: str,
        task_id: str,
        fields: Mapping[str, object],
        if_state: Sequence[str] = (),
        if_match: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        return self.port.update_task(task_id, fields, if_state=if_state, if_match=if_match, deadline=self.deadline)

    def task(self, slug: str, task_id: str) -> dict[str, Any]:
        return self.port.task(task_id, deadline=self.deadline)

    def notify(self, slug: str, text: str) -> None:
        self.port.notify(text, deadline=self.deadline)

    def question(self, slug: str, text: str, by: str, op_id: str) -> None:
        self.port.question(text, by, op_id, deadline=self.deadline)

    def comment(self, slug: str, task_id: str, text: str, by: str) -> None:
        self.port.comment(task_id, text, by, deadline=self.deadline)


@dataclass(frozen=True)
class MetricsCalls:
    """The gate log, metrics spool and process table of the spawn helpers, over the metrics and processes ports."""

    metrics: Metrics
    processes: Processes
    deadline: Deadline

    def log(self, slug: str, rows: Sequence[object]) -> None:
        self.metrics.log(slug, rows, deadline=self.deadline)

    def record(self, table: Any, rows: list[dict[str, object]]) -> list[str]:
        try:
            self.metrics.emit({table.name: rows}, deadline=self.deadline)
        except (sqlite3.Error, OSError, ValueError, KeyError, TimeoutError) as exc:
            return [f"metrics outbox failed: {exc}"]
        return []

    def table(self) -> Any:
        return self.processes.rescan(deadline=self.deadline)


@dataclass(frozen=True)
class LocalMetrics:
    """The gate log and process table of spawn helpers run outside a tick step, on this host."""

    append: Callable[[str, Any], object]
    processes: Callable[[], Any]

    def log(self, slug: str, rows: Sequence[object]) -> None:
        for row in rows:
            self.append(slug, row)

    def table(self) -> Any:
        return self.processes()
