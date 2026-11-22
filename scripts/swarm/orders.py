"""Order times: when the operator's order reached each unfinished task of a ledger."""

from collections.abc import Callable
from typing import Any

from scripts.swarm import freeze

MARK = "ordered"


def marked(row: dict[str, Any]) -> int | None:
    """The time of the row's order mark, None when it carries no well formed one."""
    match row.get(MARK):
        case {"at": int() as at} if type(at) is int:
            return at
        case _:
            return None


def stamps(doc: dict[str, Any]) -> dict[str, int]:
    """Each unfinished task's order time: the earlier of its own mark and its phase's, or the earliest time of the
    ordered work that waits on it, that it enhances or that is merged into it, through every such link in turn."""
    tasks: list[dict[str, Any]] = doc.get("tasks") or []
    phases: list[dict[str, Any]] = doc.get("phases") or []
    ordered = {phase.get("id"): at for phase in phases if (at := marked(phase)) is not None}
    found: dict[str, int] = {}
    for task in tasks:
        times = [at for at in (marked(task), ordered.get(task.get("phase"))) if at is not None]
        if times and _unfinished(task):
            found[task["id"]] = min(times)
    if not found:
        return found
    rows = {task["id"]: task for task in tasks}
    reach = _reach(doc, tasks)
    spread: dict[str, int] = {}
    for at in sorted(set(found.values())):
        seeds = [name for name, own in found.items() if own == at]
        for name in freeze.reached(seeds, rows, set(spread), reach):
            if name in rows and _unfinished(rows[name]):
                spread[name] = at
    return spread


def _unfinished(row: dict[str, Any]) -> bool:
    return row.get("state") != "done" and not row.get("out_of_scope")


def _reach(doc: dict[str, Any], tasks: list[dict[str, Any]]) -> Callable[[dict[str, Any]], list[str]]:
    """Where a task's order time goes next: the work it waits on, the task it is merged into and the plan tasks that
    enhance it; nowhere from a row that is done, out of scope or no task of the ledger."""
    waits_on = freeze.waits(doc)
    plans: dict[str, list[str]] = {}
    for task in tasks:
        if task.get("enhances"):
            plans.setdefault(task["enhances"], []).append(task["id"])

    def reach(row: dict[str, Any]) -> list[str]:
        if not row or not _unfinished(row):
            return []
        lead: list[str] = [row["merged_into"]] if row.get("merged_into") else []
        return [*waits_on(row), *lead, *plans.get(row["id"], [])]

    return reach
