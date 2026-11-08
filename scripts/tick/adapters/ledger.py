"""Ledger port adapter: the tick's ledger tools over the ledger server's export and mutate routes, each call
bounded by its deadline and every write stamped with the controller epoch of its ledger."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast
from uuid import uuid4

from scripts.swarm import lease
from scripts.swarm_ledger.api.client import ResourceClient
from scripts.swarm import timing
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


def operation(kind: str, by: str, fields: Mapping[str, object]) -> dict[str, object]:
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
        mutated: dict[str, Any] = client.mutate(self.slug, stamped)
        if mutated.get("rejected"):
            from scripts.swarm.ledger_client import LedgerRefused

            detail = "; ".join(mutated.get("_meta", {}).get("warnings", [])) or str(mutated["rejected"])
            raise LedgerRefused(f"ledger {self.slug} refused: {detail}")
        from scripts.tick import view

        view.wrote()
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
        deadline.live()
        guard: dict[str, object] = {"if_state": list(if_state)} if if_state else {}
        if if_match is not None:
            guard["if_match"] = if_match
        state = self.mutate(
            [operation("task_update", "swarm", {"item": f"tasks/{task_id}", "fields": dict(fields), **guard})],
            deadline=deadline,
        )
        tasks = cast(list[dict[str, Any]], state.get("tasks", []))
        found: dict[str, Any] = next((task for task in tasks if task["id"] == task_id), dict[str, Any]())
        return found

    def task(self, task_id: str, *, deadline: Deadline) -> dict[str, Any]:
        from urllib.error import HTTPError
        from urllib.parse import quote

        client: Any = self.clients(deadline.live())
        try:
            reply: dict[str, Any] = client.request(self.slug, f"tasks/{quote(task_id, safe='')}")
        except HTTPError as error:
            if error.code != 404:
                raise
            return {}
        return reply["data"]

    def notify(self, text: str, *, deadline: Deadline) -> None:
        deadline.live()
        self.mutate([operation("notice", "swarm", {"text": text})], deadline=deadline)

    def question(self, text: str, by: str, op_id: str, *, deadline: Deadline) -> None:
        deadline.live()
        self.mutate(
            [{**operation("add_item", by, {"list": "questions", "text": text}), "id": op_id}], deadline=deadline
        )

    def comment(self, task_id: str, text: str, by: str, *, deadline: Deadline) -> None:
        deadline.live()
        self.mutate([operation("add", by, {"thread": f"tasks/{task_id}/comments", "text": text})], deadline=deadline)


@dataclass(frozen=True)
class PassLedger:
    """Preserve the domain client's behavior while bounding each reap call by its step deadline."""

    slug: str
    client: Any

    def export(self, *, deadline: Deadline) -> dict[str, Any]:
        deadline.live()
        state: dict[str, Any] = self.client.state(self.slug)
        return state

    def mutate(self, operations: Sequence[Mapping[str, object]], *, deadline: Deadline) -> dict[str, Any]:
        deadline.live()
        state: dict[str, Any] = self.client._call(self.slug, list(operations))
        return state

    def update_task(
        self,
        task_id: str,
        fields: Mapping[str, object],
        *,
        if_state: Sequence[str] = (),
        if_match: Mapping[str, object] | None = None,
        deadline: Deadline,
    ) -> dict[str, Any]:
        deadline.live()
        guard: dict[str, object] = {"if_state": if_state} if if_state else {}
        if if_match is not None:
            guard["if_match"] = if_match
        row: dict[str, Any] = timing.timed(self.client.update_task, self.slug, task_id, fields, **guard)
        return row

    def task(self, task_id: str, *, deadline: Deadline) -> dict[str, Any]:
        deadline.live()
        row: dict[str, Any] = self.client.task(self.slug, task_id)
        return row

    def notify(self, text: str, *, deadline: Deadline) -> None:
        deadline.live()
        self.client.notify(self.slug, text)

    def question(self, text: str, by: str, op_id: str, *, deadline: Deadline) -> None:
        deadline.live()
        self.client.question(self.slug, text, by, op_id)

    def comment(self, task_id: str, text: str, by: str, *, deadline: Deadline) -> None:
        deadline.live()
        self.client.comment(self.slug, task_id, text, by)
