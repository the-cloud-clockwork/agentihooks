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


def open_order(name: str, row: dict[str, Any], at: int, seen: focus_check.View) -> dict[str, Any]:
    """One order: who filed it, when and on what proof, and the claim gate verdict on every task under it that is
    neither done nor out of scope."""
    item = f"{name}/{row['id']}"
    mark: dict[str, Any] = row[orders.MARK]
    found = focus_check.target(item, seen)
    tasks: list[dict[str, Any]] = [task for task in found["tasks"] if not orders.closed(seen.rows[task["id"]])]
    entry = {
        "item": item,
        "title": row.get("title") or "",
        "by": mark.get("by") or "",
        "at": at,
        **{key: mark[key] for key in PROOFS if key in mark},
        "claimable": sum(task["verdict"] == focus_check.CLAIMABLE for task in tasks),
        "busy": sum(task["state"] in focus_check.BUSY for task in tasks),
        "done": [task["id"] for task in focus_check.under(item, seen) if orders.done(task)],
        "tasks": tasks,
    }
    return {**entry, "line": line(entry)}


def _phases(task: dict[str, Any], rows: dict[str, dict[str, Any]]) -> tuple[Any, Any]:
    """The phases a task counts under by name: the one its row names and the one the lead it is merged into names."""
    merged: Any = task.get("merged_into")
    lead: dict[str, Any] = rows.get(merged, {})
    return task.get("phase"), lead.get("phase")


def _named(name: str, row: dict[str, Any], seen: focus_check.View) -> focus_check.View:
    """The view in which an ordered phase covers every task that counts under it by name beside those the plan graph
    puts under it: a read that carries no slices cannot climb from a task filed under one. An ordered task covers
    what the graph gives it."""
    if name != "phases":
        return seen
    item = f"{name}/{row['id']}"
    graphed = {task["id"] for task in seen.covered.get(item, [])}
    tasks = [task for task in seen.doc["tasks"] if task["id"] in graphed or row["id"] in _phases(task, seen.rows)]
    return replace(seen, covered={item: tasks})


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
                found.append(open_order(name, row, at, _named(name, row, seen)))
    return sorted(found, key=lambda entry: entry["at"])
