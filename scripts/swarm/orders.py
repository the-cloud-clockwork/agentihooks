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
    """Each unfinished task's order time: the earlier of its own mark and its open phase's, or the earliest time of
    the ordered work that waits on it, that it enhances or that is merged into it, through every such link in turn."""
    tasks: list[dict[str, Any]] = doc.get("tasks") or []
    phases: list[dict[str, Any]] = doc.get("phases") or []
    ordered = {phase.get("id"): at for phase in phases if (at := marked(phase)) is not None and not closed(phase)}
    found: dict[str, int] = {}
    for task in tasks:
        times = [at for at in (marked(task), ordered.get(task.get("phase"))) if at is not None]
        if times and not closed(task):
            found[task["id"]] = min(times)
    if not found:
        return found
    rows = {task["id"]: task for task in tasks}
    reach = _reach(doc, tasks)
    for seed, at in list(found.items()):
        waiting = reach(rows[seed])
        while waiting:
            row = rows.get(waiting.pop())
            if row is None or closed(row):
                continue
            held = found.get(row["id"])
            if held is not None and held <= at:
                continue
            found[row["id"]] = at
            waiting += reach(row)
    return found


def done(row: dict[str, Any]) -> bool:
    """Whether a phase or task row is done, by its flag or its state."""
    return bool(row.get("done") or row.get("state") == "done")


def closed(row: dict[str, Any]) -> bool:
    """Whether a phase or task row is done or out of scope, so no order stands on it or passes through it."""
    return done(row) or bool(row.get("out_of_scope"))


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
