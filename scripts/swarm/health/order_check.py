"""The orders read: every open operator order with the claim gate verdict on each task under it."""

from typing import Any

from scripts.swarm import orders
from scripts.swarm.health import focus_check

LISTS = ("phases", "tasks")
PROOFS = ("quote", "request")


def closed(row: dict[str, Any]) -> bool:
    return bool(row.get("done") or row.get("state") == "done" or row.get("out_of_scope"))


def line(entry: dict[str, Any]) -> str:
    """One plain line for an order: the work it names and how much of it is open, claimable, in flight and done."""
    head = f"{entry['item']} {entry['title']}".rstrip()
    counts = f"{len(entry['tasks'])} open, {entry['claimable']} claimable"
    return f"{head}: {counts}, {entry['busy']} claimed or in a pull request, {len(entry['done'])} done"


def order(name: str, row: dict[str, Any], at: int, seen: focus_check.View) -> dict[str, Any]:
    """One order: who filed it, when and on what proof, and the claim gate verdict on every task under it."""
    item = f"{name}/{row['id']}"
    mark: dict[str, Any] = row[orders.MARK]
    found = focus_check.target(item, seen)
    entry = {
        "item": item,
        "title": row.get("title") or "",
        "by": mark.get("by") or "",
        "at": at,
        **{key: mark[key] for key in PROOFS if key in mark},
        "claimable": found["claimable"],
        "busy": sum(task["state"] in focus_check.BUSY for task in found["tasks"]),
        "done": found["done"],
        "tasks": found["tasks"],
    }
    return {**entry, "line": line(entry)}


def report(seen: focus_check.View) -> list[dict[str, Any]]:
    """Every phase or task that carries the operator's order and is neither done nor out of scope, oldest order
    first."""
    found: list[dict[str, Any]] = []
    for name in LISTS:
        rows: list[dict[str, Any]] = seen.doc.get(name) or []
        for row in rows:
            at = orders.marked(row)
            if at is not None and not closed(row):
                found.append(order(name, row, at, seen))
    return sorted(found, key=lambda entry: entry["at"])


def read(doc: dict[str, Any], fix: str) -> list[dict[str, Any]]:
    """The ledger API's orders read."""
    return report(focus_check.view(doc, fix))
