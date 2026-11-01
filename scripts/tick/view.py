"""Ledger view per swarm, kept under the scheduler runtime folder and patched from one delta read per pass.

The view holds the tasks, metadata and phases of a ledger at one generation. Each pass asks the ledger server
for the changes of those parts since that generation; the server sends the parts in full only when its change
window no longer covers the gap.
"""

import hashlib
import urllib.error
from collections.abc import Callable
from contextvars import ContextVar
from pathlib import Path

from scripts.swarm_ledger.api.client import ResourceClient, patched, read_export_file, write_export_file

__all__ = ["CURRENT", "PARTS", "LedgerView", "Reader", "refreshed", "start", "view_file"]

PARTS = ("tasks", "_meta", "phases")

Reader = Callable[[int, tuple[str, ...]], dict]


class LedgerView:
    """Tasks, metadata and phases of one ledger at one generation."""

    def __init__(self, generation: int, state: dict) -> None:
        self.generation = generation
        self.tasks = state.get("tasks", [])
        self.meta = state.get("_meta", {})
        self.phases = state.get("phases", [])

    def read(self, domain: str, key: str) -> object | None:
        """Return the task or phase row with id ``key``, or the metadata entry ``key``, or nothing."""
        if domain == "_meta":
            return self.meta.get(key)
        rows = {"tasks": self.tasks, "phases": self.phases}.get(domain, [])
        return next((row for row in rows if row.get("id") == key), None)


CURRENT: ContextVar[LedgerView | None] = ContextVar("tick_ledger_view", default=None)


def view_file(base: str, slug: str, directory: Path) -> Path:
    """The file holding the view of ledger ``slug`` on the server at ``base``."""
    return directory / "ledger-views" / f"{hashlib.sha256(f'{base} {slug}'.encode()).hexdigest()[:32]}.json"


def refreshed(path: Path, reader: Reader) -> LedgerView:
    """The view stored at path patched with one delta read, stored back when its generation moved or a stored
    view that no longer fits was replaced by a full read."""
    stored = read_export_file(path)
    generation, held = stored if stored is not None else (0, None)
    reply = reader(generation, PARTS)
    try:
        state = patched(held, reply["changes"]) if "changes" in reply else reply["data"]
    except (KeyError, IndexError, TypeError, AttributeError):
        reply, generation = reader(0, PARTS), None
        state = reply["data"]
    if reply["generation"] != generation:
        write_export_file(path, reply["generation"], state)
    return LedgerView(reply["generation"], state)


def start(slug: str, service: bool = False) -> LedgerView | None:
    """Patch the stored view of this swarm's ledger with one delta read and hold it for the rest of the pass;
    hold nothing when the ledger server refuses named parts because it predates them."""
    from scripts.scheduler import service as scheduler
    from scripts.swarm.ledger_client import _ledger

    ledger = _ledger()
    base = ledger.base(slug)
    client = ResourceClient(base, ledger.credentials(slug, service), ledger.REQUEST_TIMEOUT)
    try:
        view = refreshed(
            view_file(base, slug, scheduler.runtime_dir()), lambda since, parts: client.changes(slug, since, parts)
        )
    except urllib.error.HTTPError as exc:
        if exc.code != 400:
            raise
        view = None
    CURRENT.set(view)
    return view
