from pathlib import Path
from typing import Any

from scripts.swarm_ledger import storage
from scripts.swarm_ledger.storage import read_document, read_flags, read_ledger

# ruleid: ledger-hook-whole-collection-read
GATE_READS = ("_meta.members", "_meta.events", "tasks", "phases", "followups", "policy")
# ok: ledger-hook-whole-collection-read
BOUNDED_READS = ("_meta.members", "_meta.events", "policy")


def whole_tasks(folder: Path, slug: str) -> Any:
    # ruleid: ledger-hook-whole-collection-read
    return read_ledger(folder, slug, "_meta.members", "tasks")


def whole_followups(folder: Path, slug: str) -> Any:
    # ruleid: ledger-hook-whole-collection-read
    return storage.read_ledger(folder, slug, "followups")


def every_ledger_phases(folder: Path) -> Any:
    # ruleid: ledger-hook-whole-collection-read
    return storage.read_ledgers(folder, "phases")


def whole_document(folder: Path, slug: str) -> Any:
    # ruleid: ledger-hook-whole-collection-read
    return read_document(folder, slug)


def whole_document_through_module(folder: Path, slug: str) -> Any:
    # ruleid: ledger-hook-whole-collection-read
    return storage.read_document(folder, slug)


def one_task(folder: Path, slug: str, task: str) -> Any:
    # ok: ledger-hook-whole-collection-read
    return read_ledger(folder, slug, "_meta.members", f"tasks/{task}")


def close_flags(folder: Path, slug: str) -> Any:
    # ok: ledger-hook-whole-collection-read
    return read_flags(folder, slug, ("phases", "followups"), ("done", "out_of_scope"))


def handed_tasks(state: dict[str, Any]) -> Any:
    # ok: ledger-hook-whole-collection-read
    return state.get("tasks", [])
