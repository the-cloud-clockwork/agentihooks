"""Ledger port adapter: the tick's ledger tools over the ledger server's export and mutate routes, each call
bounded by its deadline and every write stamped with the controller epoch of its ledger."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast
from uuid import uuid4

from scripts.swarm import lease
from scripts.swarm_ledger.api.client import ResourceClient
from scripts.tick.deadline import Deadline

__all__ = ["ClientLedger", "resource_client"]

Clients = Callable[[float], ResourceClient]


def resource_client(slug: str, service: bool = False) -> Clients:
    """Build clients for ledger slug whose request timeout never outlasts the deadline left, served in process
    over the shared database while a database pass is in scope."""
    from scripts.swarm.ledger_client import _ledger  # pyright: ignore[reportPrivateUsage]
    from scripts.tick.adapters import database_ledger

    ledger: Any = _ledger()

    def build(remaining: float) -> ResourceClient:
        from scripts.swarm.ledger_client import OFFLINE_READ, LedgerOffline, offline

        if offline():
            raise LedgerOffline(f"ledger {slug}: {OFFLINE_READ}")
        base: str = ledger.base(slug)
        timeout: float = min(ledger.REQUEST_TIMEOUT, remaining)
        if database_ledger.serving():
            return database_ledger.client(base, timeout)
        credentials: dict[str, str] = ledger.credentials(slug, service)
        return ResourceClient(base, credentials, timeout)

    return build


def operation(kind: str, by: str, **fields: object) -> dict[str, object]:
    return {"op": kind, "id": f"{kind}-{uuid4().hex[:10]}", "by": by, **fields}


@dataclass(frozen=True)
class ClientLedger:
    """Read the export of one ledger and apply operations to it through the mutate route."""

    slug: str
    clients: Clients

    def export(self, *, deadline: Deadline) -> dict[str, object]:
        client: Any = self.clients(deadline.live())
        exported: dict[str, object] = client.export(self.slug)
        return exported

    def mutate(self, operations: Sequence[Mapping[str, object]], *, deadline: Deadline) -> dict[str, object]:
        client: Any = self.clients(deadline.live())
        epoch = lease.epoch_for(self.slug)
        stamped = [
            dict(operation) if epoch is None else {**operation, "controller_epoch": epoch} for operation in operations
        ]
        mutated: dict[str, object] = client.mutate(self.slug, stamped)
        return mutated

    def update_task(
        self,
        task_id: str,
        fields: Mapping[str, object],
        *,
        if_state: Sequence[str] = (),
        if_match: Mapping[str, object] | None = None,
        deadline: Deadline,
    ) -> dict[str, Any]:
        guard: dict[str, object] = {"if_state": list(if_state)} if if_state else {}
        if if_match is not None:
            guard["if_match"] = if_match
        state = self.mutate(
            [operation("task_update", "swarm", item=f"tasks/{task_id}", fields=dict(fields), **guard)],
            deadline=deadline,
        )
        tasks = cast(list[dict[str, Any]], state.get("tasks", []))
        found: dict[str, Any] = next((task for task in tasks if task["id"] == task_id), dict[str, Any]())
        return found
