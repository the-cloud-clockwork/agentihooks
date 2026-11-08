"""Ledger view per swarm, kept under the scheduler runtime folder and patched from one delta read per pass.

The view holds the tasks, metadata and phases of a ledger at one generation. Each pass asks the ledger server
for the changes of those parts since that generation; the server sends the parts in full only when its change
window no longer covers the gap.
"""

import hashlib
import os
import sys
import urllib.error
from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, cast

from scripts.swarm.ledger_client import LedgerClient, LedgerUnreachable
from scripts.swarm_ledger.api.client import ResourceClient, patched, read_export_file, write_export_file

__all__ = [
    "CURRENT",
    "PARTS",
    "READ_ONLY",
    "FrozenDict",
    "FrozenList",
    "Held",
    "REFRESH",
    "LedgerView",
    "Reader",
    "current",
    "document",
    "freeze",
    "frozen_views",
    "holding",
    "offline",
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

    def __reduce__(self) -> tuple[type[dict[str, Any]], tuple[dict[str, Any]]]:
        """Copy and pickle as a plain dict."""
        return dict, (dict(self),)


class FrozenList(list[Any]):
    """A list of the view that refuses every change and copies to a plain list."""

    def __reduce__(self) -> tuple[type[list[Any]], tuple[list[Any]]]:
        """Copy and pickle as a plain list."""
        return list, (list(self),)


for _name in ("__setitem__", "__delitem__", "__ior__", "clear", "pop", "popitem", "setdefault", "update"):
    setattr(FrozenDict, _name, staticmethod(_refuse))
for _name in (
    *("__setitem__", "__delitem__", "__iadd__", "__imul__", "append", "extend", "insert", "pop", "remove", "clear"),
    *("sort", "reverse"),
):
    setattr(FrozenList, _name, staticmethod(_refuse))


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
REFRESH: ContextVar[tuple[str, Callable[[], LedgerView]] | None] = ContextVar("tick_ledger_refresh", default=None)


@dataclass
class Held:
    """The view a pass holds, how to read it again and whether a ledger write left it stale."""

    view: LedgerView
    renew: Callable[[], LedgerView]
    stale: bool = False


HELD: ContextVar[Held | None] = ContextVar("tick_ledger_held", default=None)
OFFLINE: ContextVar[list[bool] | None] = ContextVar("tick_ledger_offline", default=None)
SNAPSHOT_TIMEOUT_S = 3.0


def offline() -> bool:
    """True while the pass runs from its stored view because the ledger server is slow or not answering."""
    return bool(OFFLINE.get())


def stored_view(slug: str, path: Path) -> LedgerView:
    """The last good view of ledger ``slug`` stored at ``path``, or LedgerUnreachable when no pass stored one."""
    stored = read_export_file(path)
    if stored is None:
        raise LedgerUnreachable(f"ledger {slug}: the server is not answering and no earlier pass stored its view")
    return LedgerView(*stored)


def _offline(slug: str, view: LedgerView, exc: BaseException) -> LedgerView:
    """Mark the pass offline and return ``view``, the last good view it runs from until the pass ends."""
    flag = OFFLINE.get()
    if flag is None:
        OFFLINE.set([True])
    else:
        flag[:] = [True]
    print(f"ledger {slug} is not answering ({exc}); the pass runs from its view at {view.generation}", file=sys.stderr)
    return view


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
    """Patch the stored view of this swarm's ledger with one delta read bound by ``SNAPSHOT_TIMEOUT_S`` and hold it
    for the rest of the pass; hold the stored view and mark the pass offline when the server is slow or not
    answering; hold nothing when the ledger server refuses named parts because it predates them."""
    from scripts.scheduler import service as scheduler
    from scripts.swarm.ledger_client import _ledger  # pyright: ignore[reportPrivateUsage]

    ledger: Any = _ledger()
    base: str = ledger.base(slug)
    client = ResourceClient(base, ledger.credentials(slug, service), SNAPSHOT_TIMEOUT_S)
    path = view_file(base, slug, scheduler.runtime_dir())
    kept: list[LedgerView] = []

    def refresh() -> LedgerView:
        """The held view patched with one delta read, or the last view held once the server stopped answering."""
        if offline() and kept:
            return kept[0]
        try:
            fresh = refreshed(path, lambda since, parts: client.changes(slug, since, parts))
        except urllib.error.HTTPError as exc:
            if exc.code < 500 or not kept:
                raise
            fresh = _offline(slug, kept[0], exc)
        except OSError as exc:
            if not kept:
                raise
            fresh = _offline(slug, kept[0], exc)
        kept[:] = [fresh]
        return fresh

    try:
        view = refresh()
    except urllib.error.HTTPError as exc:
        if exc.code == 400:
            print(f"ledger view of {slug} skipped: the ledger server refused named parts", file=sys.stderr)
            view = None
        elif exc.code >= 500:
            view = _offline(slug, stored_view(slug, path), exc)
        else:
            raise
    except OSError as exc:
        view = _offline(slug, stored_view(slug, path), exc)
    if view is not None:
        kept[:] = [view]
    CURRENT.set(view)
    HELD.set(None if view is None else Held(view, refresh))
    REFRESH.set(None if view is None else (slug, refresh))
    return view


def document(slug: str) -> dict[str, Any] | None:
    """The whole document of ledger ``slug`` from the pass's held view, patched by one delta read only when a ledger
    write left it stale or the pass holds no record of it, or nothing when the pass holds no view of that ledger."""
    held = REFRESH.get()
    if held is None or held[0] != slug:
        return None
    kept = HELD.get()
    if kept is not None and not kept.stale:
        return kept.view.document
    view = held[1]()
    CURRENT.set(view)
    if kept is not None:
        kept.view, kept.stale = view, False
    return view.document


@contextmanager
def holding() -> Generator[None]:
    """Hold no view on entry and drop whatever view the block started on exit, even when the block is left from
    another thread's context than the one that entered it."""
    current_token, refresh_token, held_token = CURRENT.set(None), REFRESH.set(None), HELD.set(None)
    offline_token = OFFLINE.set([])
    try:
        yield
    finally:
        _reset(OFFLINE, offline_token)
        _reset(HELD, held_token)
        _reset(REFRESH, refresh_token)
        _reset(CURRENT, current_token)


def _reset(var: ContextVar[Any], token: Token[Any]) -> None:
    """Restore ``var`` to its value before ``token``, or to nothing when ``token`` came from another context."""
    try:
        var.reset(token)
    except ValueError:
        var.set(None)
