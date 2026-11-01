"""Ledger view per swarm, kept under the scheduler runtime folder and patched from one delta read per pass.

The view holds the tasks, metadata and phases of a ledger at one generation. Each pass asks the ledger server
for the changes of those parts since that generation; the server sends the parts in full only when its change
window no longer covers the gap.
"""

import hashlib
import os
import sys
import urllib.error
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, NoReturn, cast

from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm_ledger.api.client import ResourceClient, patched, read_export_file, write_export_file

__all__ = [
    "CURRENT",
    "PARTS",
    "READ_ONLY",
    "FrozenDict",
    "FrozenList",
    "Held",
    "LedgerView",
    "Reader",
    "current",
    "freeze",
    "frozen_views",
    "refreshed",
    "start",
    "view_file",
    "wrote",
]

READ_ONLY = "the ledger view is read only: write through the ledger adapter and read the next view"


def _refuse(*_args: object, **_kwargs: object) -> NoReturn:
    """Refuse a change to a frozen part of the view."""
    raise TypeError(READ_ONLY)


class FrozenDict(dict[str, Any]):
    """A mapping of the view that refuses every change and copies to a plain dict."""

    __setitem__ = __delitem__ = __ior__ = staticmethod(_refuse)  # pyright: ignore[reportAssignmentType, reportIncompatibleMethodOverride]
    clear = pop = popitem = setdefault = update = staticmethod(_refuse)  # pyright: ignore[reportAssignmentType, reportIncompatibleMethodOverride]

    def __reduce__(self) -> tuple[type[dict[str, Any]], tuple[dict[str, Any]]]:
        """Copy and pickle as a plain dict."""
        return dict, (dict(self),)


class FrozenList(list[Any]):
    """A list of the view that refuses every change and copies to a plain list."""

    __setitem__ = __delitem__ = __iadd__ = __imul__ = staticmethod(_refuse)  # pyright: ignore[reportAssignmentType, reportIncompatibleMethodOverride]
    append = extend = insert = pop = remove = clear = sort = reverse = staticmethod(_refuse)  # pyright: ignore[reportAssignmentType, reportIncompatibleMethodOverride]

    def __reduce__(self) -> tuple[type[list[Any]], tuple[list[Any]]]:
        """Copy and pickle as a plain list."""
        return list, (list(self),)


def freeze(value: object) -> object:
    """Return ``value`` with every dict and list in it, at any depth, replaced by one that refuses changes."""
    if isinstance(value, dict):
        return FrozenDict({key: freeze(item) for key, item in cast(dict[str, object], value).items()})
    if isinstance(value, list):
        return FrozenList(freeze(item) for item in cast(list[object], value))
    return value


def frozen_views() -> bool:
    """True under CI, where every view is deep frozen so a step changing it fails its test."""
    return os.environ.get("CI", "").lower() in ("1", "true", "yes")


PARTS = ("tasks", "_meta", "phases", "freezes", "priorities", "followups", "alerts", "overview")

Reader = Callable[[int, tuple[str, ...]], dict[str, Any]]


class LedgerView:
    """Tasks, metadata and phases of one ledger at one generation."""

    def __init__(self, generation: int, state: dict[str, Any]) -> None:
        """Hold ``state`` at ``generation``, deep frozen under CI."""
        if frozen_views():
            state = cast(dict[str, Any], freeze(state))
        self.generation = generation
        self.tasks: list[dict[str, Any]] = state.get("tasks", [])
        self.meta: dict[str, Any] = state.get("_meta", {})
        self.phases: list[dict[str, Any]] = state.get("phases", [])
        self.document = state

    def read(self, domain: str, key: str) -> object | None:
        """Return the task or phase row with id ``key``, or the metadata entry ``key``, or nothing."""
        if domain == "_meta":
            return self.meta.get(key)
        rows = {"tasks": self.tasks, "phases": self.phases}.get(domain, [])
        return next((row for row in rows if row.get("id") == key), None)


CURRENT: ContextVar[LedgerView | None] = ContextVar("tick_ledger_view", default=None)


@dataclass
class Held:
    """The view a pass holds, how to read it again and whether a ledger write left it stale."""

    view: LedgerView
    renew: Callable[[], LedgerView]
    stale: bool = False


HELD: ContextVar[Held | None] = ContextVar("tick_ledger_held", default=None)


def wrote() -> None:
    """Mark the held view stale after a ledger write, so the next step reads a fresh view."""
    if (held := HELD.get()) is not None:
        held.stale = True


def current() -> LedgerView | None:
    """The view the pass holds, patched by a fresh delta read first when a ledger write left it stale."""
    held = HELD.get()
    if held is None:
        return CURRENT.get()
    if held.stale:
        held.view, held.stale = held.renew(), False
    return held.view


def view_file(base: str, slug: str, directory: Path) -> Path:
    """The file holding the view of ledger ``slug`` on the server at ``base``."""
    return directory / "ledger-views" / f"{hashlib.sha256(f'{base} {slug}'.encode()).hexdigest()[:32]}.json"


def refreshed(path: Path, reader: Reader) -> LedgerView:
    """The view stored at path patched with one delta read, stored back when its generation moved or a stored
    view that no longer fits was replaced by a full read."""
    stored = read_export_file(path)
    generation, held = stored if stored is not None else (0, {})
    reply = reader(generation, PARTS)
    try:
        state = patched(held, reply["changes"]) if "changes" in reply else reply["data"]
    except (KeyError, IndexError, TypeError, AttributeError):
        reply, generation = reader(0, PARTS), None
        state = reply["data"]
    if reply["generation"] != generation:
        write_export_file(path, reply["generation"], state)
    return LedgerView(reply["generation"], state)


def reads(ledger: object) -> bool:
    """True for the ledger client a pass reads the view through; never for a subclass standing in for it."""
    return type(ledger) is LedgerClient


def start(slug: str, service: bool) -> LedgerView | None:
    """Patch the stored view of this swarm's ledger with one delta read and hold it for the rest of the pass;
    hold nothing when the ledger server refuses named parts because it predates them."""
    from scripts.scheduler import service as scheduler
    from scripts.swarm.ledger_client import _ledger  # pyright: ignore[reportPrivateUsage]

    ledger: Any = _ledger()
    base: str = ledger.base(slug)
    client = ResourceClient(base, ledger.credentials(slug, service), ledger.REQUEST_TIMEOUT)
    renew = partial(
        refreshed,
        view_file(base, slug, scheduler.runtime_dir()),
        lambda since, parts: client.changes(slug, since, parts),
    )
    try:
        view = renew()
    except urllib.error.HTTPError as exc:
        if exc.code != 400:
            raise
        print(f"ledger view of {slug} skipped: the ledger server refused named parts", file=sys.stderr)
        view = None
    CURRENT.set(view)
    HELD.set(None if view is None else Held(view, renew))
    return view
