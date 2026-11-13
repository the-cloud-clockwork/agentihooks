"""Ledger view per swarm, kept under the scheduler runtime folder and patched from one delta read per pass.

The view holds the tasks, metadata and phases of a ledger at one generation. Each pass asks the ledger server
for the changes of those parts since that generation; the server sends the parts in full only when its change
window no longer covers the gap.
"""

import hashlib
import os
import sys
import time
import urllib.error
from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, cast

from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm_ledger.api.client import (
    ResourceClient,
    last_revision,
    patched,
    read_export_file,
    write_export_file,
)
from scripts.swarm_ledger.api.schemas import LEDGER_PARTS
from scripts.swarm_ledger.repository import live_rows

__all__ = [
    "CURRENT",
    "FRESH_VIEW_S",
    "PARTS",
    "READ_ONLY",
    "FrozenDict",
    "FrozenList",
    "Held",
    "REFRESH",
    "LedgerView",
    "Reader",
    "counted",
    "current",
    "document",
    "freeze",
    "fresh",
    "frozen_views",
    "hold_stored",
    "holding",
    "queued",
    "refreshed",
    "slow",
    "start",
    "stored",
    "timed_out",
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


PARTS = LEDGER_PARTS
FRESH_VIEW_S = 300.0
ROWS_READ = "rows_read"
TICK_SCOPE = live_rows.Scope(closed=True, frozen=True)

Reader = Callable[[int, tuple[str, ...], int | None], dict[str, Any]]


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
        self.frozen_ids: frozenset[str] = frozenset(state.get(live_rows.FROZEN_IDS) or ())
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
    offline: bool = False
    slow: bool = False
    path: Path | None = None


HELD: ContextVar[Held | None] = ContextVar("tick_ledger_held", default=None)


def wrote() -> None:
    """Mark the held view stale after a ledger write, so the next step reads a fresh view."""
    if (held := HELD.get()) is not None:
        held.stale = True


def offline() -> bool:
    """Whether the held view's server stopped answering in any step of this pass."""
    held = HELD.get()
    return held is not None and held.offline


def slow() -> bool:
    """Whether the held view's server last failed a read by answering too slowly rather than refusing it."""
    held = HELD.get()
    return held is not None and held.slow


def current() -> LedgerView | None:
    """The view the pass holds, patched by a fresh delta read first when a ledger write left it stale."""
    held = HELD.get()
    if held is None:
        return CURRENT.get()
    if held.stale:
        held.view, held.stale = held.renew(), False
    return held.view


def view_file(base: str, slug: str, directory: Path, scope: live_rows.Scope | None = None) -> Path:
    """The file holding the view of ledger ``slug`` on the server at ``base`` read with ``scope``."""
    scope = TICK_SCOPE if scope is None else scope
    key = f"{base} {slug} live closed={scope.closed} frozen={scope.frozen}"
    return directory / "ledger-views" / f"{hashlib.sha256(key.encode()).hexdigest()[:32]}.json"


def refreshed(path: Path, reader: Reader) -> LedgerView:
    """The view stored at path patched with one delta read, stored back when its generation moved or a stored
    view that no longer fits was replaced by a full read."""
    exported = read_export_file(path)
    generation, held = exported if exported is not None else (0, {})
    reply = reader(generation, PARTS, last_revision(held))
    try:
        state = patched(held, reply["changes"]) if "changes" in reply else reply["data"]
    except (KeyError, IndexError, TypeError, AttributeError):
        reply, generation = reader(0, PARTS, None), None
        state = reply["data"]
    if exported is None or reply["generation"] != generation:
        write_export_file(path, reply["generation"], state)
    return counted(LedgerView(reply["generation"], state))


def counted(view: LedgerView) -> LedgerView:
    """``view``, its task rows counted on the running pass's record as the most rows one read of the pass held."""
    from scripts.swarm import timing

    record = timing.PASS.get()
    if record is not None:
        record[ROWS_READ] = max(int(record.get(ROWS_READ, 0)), len(view.tasks))
    return view


def reads(ledger: object) -> bool:
    """True for the ledger client a pass reads the view through; never for a subclass standing in for it."""
    return type(ledger) is LedgerClient


def start(slug: str, service: bool, timeout: float | None = None) -> LedgerView | None:
    """Patch the stored view of this swarm's ledger with one delta read and hold it for the rest of the pass;
    hold nothing when the ledger server refuses named parts because it predates them."""
    from scripts.scheduler import service as scheduler
    from scripts.swarm.ledger_client import _ledger  # pyright: ignore[reportPrivateUsage]

    ledger: Any = _ledger()
    base: str = ledger.base(slug)
    client = ResourceClient(base, ledger.credentials(slug, service), timeout or ledger.REQUEST_TIMEOUT)
    path = view_file(base, slug, scheduler.runtime_dir())

    def refresh() -> LedgerView:
        """The held view patched with one delta read, dropping the task fields offline passes queued."""
        answered = refreshed(path, lambda since, parts, after: client.changes(slug, since, parts, TICK_SCOPE, after))
        queued_file(path).unlink(missing_ok=True)
        return answered

    try:
        view = refresh()
    except urllib.error.HTTPError as exc:
        if exc.code != 400:
            raise
        print(f"ledger view of {slug} skipped: the ledger server refused named parts", file=sys.stderr)
        queued_file(path).unlink(missing_ok=True)
        view = None
    _hold(slug, view, _kept(refresh), path)
    return view


def _kept(refresh: Callable[[], LedgerView]) -> Callable[[], LedgerView]:
    """``refresh``, answering with the view the pass already holds when the server stops answering mid pass."""

    def renew() -> LedgerView:
        """The held view patched with one delta read, or the held view as it is while the server is not answering."""
        try:
            return refresh()
        except urllib.error.HTTPError:
            raise
        except OSError as exc:
            stopped(exc)
            held: Held = HELD.get()  # pyright: ignore[reportAssignmentType]
            return held.view

    return renew


def stopped(exc: BaseException | None) -> None:
    """Leave the held view offline for the rest of the pass when ``exc`` shows the server stopped answering, slow
    when it only timed out; a refusal or any other failure leaves it as it is."""
    held = HELD.get()
    if held is None or not isinstance(exc, OSError) or isinstance(exc, urllib.error.HTTPError):
        return
    held.offline, held.slow = True, timed_out(exc)


def timed_out(exc: OSError) -> bool:
    """True when a ledger read failed because the server answered too slowly, not because it refused or is gone."""
    return isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError)


def _hold(slug: str, view: LedgerView | None, renew: Callable[[], LedgerView], path: Path | None = None) -> None:
    """Hold ``view`` of ledger ``slug`` for the rest of the pass, read again through ``renew``."""
    CURRENT.set(view)
    HELD.set(None if view is None else Held(view, renew, path=path))
    REFRESH.set(None if view is None else (slug, renew))


def stored(slug: str) -> LedgerView | None:
    """The last good view of this swarm's ledger stored on this host with the task fields an offline pass queued
    since applied, or nothing when the host stores none."""
    return _stored_at(_stored_file(slug))


def _stored_at(path: Path) -> LedgerView | None:
    """The view stored at ``path`` with the task fields kept beside it applied, or nothing when none is stored."""
    found = read_export_file(path)
    if found is None:
        return None
    generation, state = found
    kept = read_export_file(queued_file(path))
    if kept is not None and "tasks" in state:
        state["tasks"] = [{**task, **kept[1].get(task.get("id"), {})} for task in state["tasks"]]
    return LedgerView(generation, state)


def _stored_file(slug: str) -> Path:
    """The file holding the last good view of this swarm's ledger on this host."""
    from scripts.scheduler import service as scheduler
    from scripts.swarm.ledger_client import _ledger  # pyright: ignore[reportPrivateUsage]

    ledger: Any = _ledger()
    base: str = ledger.base(slug)
    return view_file(base, slug, scheduler.runtime_dir())


def queued_file(path: Path) -> Path:
    """The file beside stored view ``path`` keeping the task fields offline passes queued until the server answers."""
    return path.with_name(f"{path.stem}.queued.json")


def fresh(slug: str) -> bool:
    """True when this host stored a view of this swarm's ledger within ``FRESH_VIEW_S``, recent enough for the
    server to still answer a delta read after it."""
    from scripts.scheduler import service as scheduler
    from scripts.swarm.ledger_client import _ledger  # pyright: ignore[reportPrivateUsage]

    ledger: Any = _ledger()
    base: str = ledger.base(slug)
    try:
        written = view_file(base, slug, scheduler.runtime_dir()).stat().st_mtime
    except OSError:
        return False
    return time.time() - written < FRESH_VIEW_S


def hold_stored(slug: str) -> LedgerView | None:
    """Hold the last good view of this swarm's ledger stored on this host, read again as itself, or nothing when
    the host stores none."""
    path = _stored_file(slug)
    view = _stored_at(path)
    if view is not None:
        _hold(slug, view, lambda: HELD.get().view, path)  # pyright: ignore[reportOptionalMemberAccess]
    return view


def queued(ops: list[dict[str, Any]]) -> dict[str, Any]:
    """The held document with the task fields of queued ``ops`` applied, held as the view the rest of the pass
    reads and kept beside the stored view for later offline passes, or an empty document when the pass holds no
    view."""
    held = HELD.get()
    if held is None:
        return {}
    fields = {op["item"].removeprefix("tasks/"): op["fields"] for op in ops if op.get("op") == "task_update"}
    document = dict(held.view.document)
    document["tasks"] = [{**task, **fields.get(task.get("id"), {})} for task in held.view.tasks]
    held.view = LedgerView(held.view.generation, document)
    if held.path is not None:
        _keep_queued(queued_file(held.path), held.view.generation, fields)
    return document


def _keep_queued(path: Path, generation: int, fields: dict[str, dict[str, Any]]) -> None:
    """Merge the queued task ``fields`` into those kept at ``path``."""
    kept = read_export_file(path)
    merged: dict[str, Any] = dict(kept[1]) if kept is not None else {}
    for task, values in fields.items():
        merged[task] = {**merged.get(task, {}), **values}
    write_export_file(path, generation, merged)


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
    try:
        yield
    finally:
        _reset(HELD, held_token)
        _reset(REFRESH, refresh_token)
        _reset(CURRENT, current_token)


def _reset(var: ContextVar[Any], token: Token[Any]) -> None:
    """Restore ``var`` to its value before ``token``, or to nothing when ``token`` came from another context."""
    try:
        var.reset(token)
    except ValueError:
        var.set(None)
