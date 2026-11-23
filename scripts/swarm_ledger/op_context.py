"""The op context a registered ledger op module receives: what an op reads and writes of one sync.

``ledger_core.Context`` is the one concrete class. This module imports nothing of the ledger, so every op module
names the type without an import cycle: the core imports the op modules, and they import this."""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class OpContext(Protocol):
    """One sync as its ops see it: the clock, the revision being written, the stamps, the events recorded so far,
    the refusals, the ledger's meta record and slug, and whether the document changed without an event."""

    at: int
    rev: int
    events: list[dict[str, Any]]
    refused: list[str]
    meta: dict[str, Any]
    slug: str
    dirty: bool
    stamps: dict[str, dict[str, Any]]

    def author(self, name: str) -> str: ...

    def record(self, by: str, kind: str, target: str, **extra: Any) -> None: ...

    def stamp(self, path: str, by: str) -> None: ...
