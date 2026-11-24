"""The op context a registered ledger op module receives: what an op reads and writes of one sync.

``ledger_core.Context`` is the one concrete class. This module imports nothing of the ledger, so every op module
names the type without an import cycle: the core imports the op modules, and they import this."""

from collections.abc import Callable
from typing import Any, Concatenate, Protocol, runtime_checkable


@runtime_checkable
class OpContext(Protocol):
    """One sync as its ops see it: the clock, the revision being written, the stamps, the events recorded so far,
    the refusals, the ledger's meta record and slug, and whether the document changed without an event. ``record``
    takes who acted, the kind of the event and its target, then every further field of the event by name."""

    at: int
    rev: int
    events: list[dict[str, Any]]
    refused: list[str]
    meta: dict[str, Any]
    slug: str
    dirty: bool
    stamps: dict[str, dict[str, Any]]
    record: Callable[Concatenate[str, str, str, ...], None]

    def author(self, name: str) -> str: ...

    def stamp(self, path: str, by: str) -> None: ...
