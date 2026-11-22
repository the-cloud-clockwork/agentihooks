"""Order times: when the operator's order reached each open task of a ledger."""

from collections.abc import Callable
from typing import Any, cast

from scripts.swarm import freeze

MARK = "ordered"


def marked(row: dict[str, Any]) -> int | None:
    """The time of the row's order mark, None when it carries no well formed one."""
    mark: object = row.get(MARK)
    if not isinstance(mark, dict):
        return None
    at = cast("dict[str, object]", mark).get("at")
    return at if type(at) is int else None


def stamps(doc: dict[str, Any]) -> dict[str, int]:
    """Each open task's order time: the earlier of its own mark and its phase's, or the earliest time of the ordered
    work that waits on it, that it enhances or that is merged into it."""
    tasks: list[dict[str, Any]] = doc.get("tasks") or []
    phases: list[dict[str, Any]] = doc.get("phases") or []
    ordered = {phase.get("id"): at for phase in phases if (at := marked(phase)) is not None}
    found: dict[str, int] = {}
    for task in tasks:
        times = [at for at in (marked(task), ordered.get(task.get("phase"))) if at is not None]
        if times and _open(task):
            found[task["id"]] = min(times)
    if not found:
        return found
    rows = {task["id"]: task for task in tasks}
    reach = _reach(doc, tasks)
    for seed, at in list(found.items()):
        waiting = reach(rows[seed])
        while waiting:
            row = rows.get(waiting.pop())
            if row is None or not _open(row) or found.get(row["id"], at + 1) <= at:
                continue
            found[row["id"]] = at
            waiting += reach(row)
    return found


def _open(row: dict[str, Any]) -> bool:
    return row.get("state") != "done" and not row.get("out_of_scope")


def _reach(doc: dict[str, Any], tasks: list[dict[str, Any]]) -> Callable[[dict[str, Any]], list[str]]:
    """Where a task's order time goes next: the work it waits on, the task it is merged into and the plan tasks that
    enhance it."""
    waits_on = freeze.waits(doc)
    plans: dict[str, list[str]] = {}
    for task in tasks:
        if task.get("enhances"):
            plans.setdefault(task["enhances"], []).append(task["id"])

    def reach(row: dict[str, Any]) -> list[str]:
        lead: list[str] = [row["merged_into"]] if row.get("merged_into") else []
        return [*waits_on(row), *lead, *plans.get(row["id"], [])]

    return reach
