"""Ledger port adapter: the tick's ledger tools over the ledger server's export and mutate routes, each call
bounded by its deadline and every write stamped with the controller epoch of its ledger."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from scripts.swarm import lease
from scripts.swarm_ledger.api.client import ResourceClient
from scripts.tick.deadline import Deadline

__all__ = ["ClientLedger", "resource_client"]

Clients = Callable[[float], ResourceClient]


def resource_client(slug: str, service: bool = False) -> Clients:
    """Build clients for ledger slug whose request timeout never outlasts the deadline left."""
    from scripts.swarm.ledger_client import _ledger  # pyright: ignore[reportPrivateUsage]

    ledger: Any = _ledger()

    def build(remaining: float) -> ResourceClient:
        base: str = ledger.base(slug)
        credentials: dict[str, str] = ledger.credentials(slug, service)
        timeout: float = ledger.REQUEST_TIMEOUT
        return ResourceClient(base, credentials, min(timeout, remaining))

    return build


@dataclass(frozen=True)
class ClientLedger:
    """Read the export of one ledger and apply operations to it through the mutate route."""

    slug: str
    clients: Clients

    def export(self, *, deadline: Deadline) -> dict[str, object]:
        client: Any = self.clients(deadline.live())
        return cast(dict[str, object], client.export(self.slug))

    def mutate(self, operations: Sequence[Mapping[str, object]], *, deadline: Deadline) -> dict[str, object]:
        client: Any = self.clients(deadline.live())
        epoch = lease.epoch_for(self.slug)
        stamped = [
            dict(operation) if epoch is None else {**operation, "controller_epoch": epoch} for operation in operations
        ]
        return cast(dict[str, object], client.mutate(self.slug, stamped))
