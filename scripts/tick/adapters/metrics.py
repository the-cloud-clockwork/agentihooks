"""Metrics port adapter: measurements appended to the local metrics spool in one transaction, admitted only while its deadline holds."""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING, Any, Protocol, cast

from scripts.tick.deadline import Deadline

if TYPE_CHECKING:
    from scripts.swarm.metrics_outbox import Table

__all__ = ["SpoolMetrics"]


class Spool(Protocol):
    def append_many(
        self, batches: list[tuple["Table", list[dict[str, object]]]], checkpoint: Callable[..., None]
    ) -> None: ...


def _no_checkpoint(db: object) -> None:
    """Leave the spool transaction without an extra checkpoint write."""


@dataclass(frozen=True)
class SpoolMetrics:
    box: Spool
    tables: Mapping[str, "Table"]

    def emit(self, measurements: Mapping[str, object], *, deadline: Deadline) -> object:
        deadline.live()
        batches: list[tuple[Table, list[dict[str, object]]]] = []
        for name, rows in measurements.items():
            if name not in self.tables:
                raise KeyError(f"unknown metrics table: {name}")
            batches.append((self.tables[name], list(cast(Iterable[dict[str, object]], rows))))
        self.box.append_many(batches, _no_checkpoint)
        return sum(len(rows) for _, rows in batches)

    def log(self, slug: str, rows: Sequence[object], *, deadline: Deadline) -> object:
        """Append each gate log row of swarm ``slug`` to its gate log and return how many landed."""
        gate_log: Any = import_module("scripts.gates.log")
        deadline.live()
        for row in rows:
            gate_log.append(slug, row)
        return len(rows)
