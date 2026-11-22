"""The orders read: every open operator order with the claim gate verdict on each unfinished task under it."""

from dataclasses import replace
from typing import Any

from scripts.swarm import orders
from scripts.swarm.health import focus_check

LISTS = ("phases", "tasks")
PROOFS = ("quote", "request")


def line(entry: dict[str, Any]) -> str:
    """One plain line for an order: the work it names and how much of it is unfinished, claimable, in flight and
    done."""
    head = f"{entry['item']} {entry['title']}".rstrip()
    counts = f"{len(entry['tasks'])} unfinished, {entry['claimable']} claimable"
    return f"{head}: {counts}, {entry['busy']} claimed or in a pull request, {len(entry['done'])} done"


def _named_phases(task: dict[str, Any], rows: dict[str, dict[str, Any]]) -> tuple[Any, Any]:
    """The phase a task's row names and the phase the row of the lead it is merged into names."""
    merged: Any = task.get("merged_into")
    lead: dict[str, Any] = rows.get(merged, {})
    return task.get("phase"), lead.get("phase")


def _under(name: str, item: str, row: dict[str, Any], seen: focus_check.View) -> list[dict[str, Any]]:
    """Every task under an order: the tasks the plan graph puts there or, for a phase read without its slices, the
    tasks that name it, themselves or through the lead they are merged into."""
    if name != "phases" or "slices" in seen.doc:
        return focus_check.under(item, seen)
    return [task for task in seen.doc["tasks"] if row["id"] in _named_phases(task, seen.rows)]


def open_order(name: str, row: dict[str, Any], at: int, seen: focus_check.View) -> dict[str, Any]:
    """One order: who filed it, when and on what proof, and the claim gate verdict on every task under it that is
    neither done nor out of scope."""
    item = f"{name}/{row['id']}"
    mark: dict[str, Any] = row[orders.MARK]
    members = _under(name, item, row, seen)
    found = focus_check.target(item, replace(seen, covered={**seen.covered, item: members}))
    tasks: list[dict[str, Any]] = [task for task in found["tasks"] if not orders.closed(seen.rows[task["id"]])]
    entry = {
        "item": item,
        "title": row.get("title") or "",
        "by": mark.get("by") or "",
        "at": at,
        **{key: mark[key] for key in PROOFS if key in mark},
        "claimable": sum(task["verdict"] == focus_check.CLAIMABLE for task in tasks),
        "busy": sum(task["state"] in focus_check.BUSY for task in tasks),
        "done": [task["id"] for task in members if orders.done(task)],
        "tasks": tasks,
    }
    return {**entry, "line": line(entry)}


def read(doc: dict[str, Any], fix: str) -> list[dict[str, Any]]:
    """The ledger API's orders read: every phase or task that carries the operator's order and is neither done nor
    out of scope, oldest order first."""
    seen = focus_check.view(doc, fix)
    found: list[dict[str, Any]] = []
    for name in LISTS:
        rows: list[dict[str, Any]] = seen.doc.get(name) or []
        for row in rows:
            at = orders.marked(row)
            if at is not None and not orders.closed(row):
                found.append(open_order(name, row, at, seen))
    return sorted(found, key=lambda entry: entry["at"])
