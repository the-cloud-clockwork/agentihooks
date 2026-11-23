"""The focus sanity check: the claim gate verdict on every task under a focus or unfreeze target.

`swarm focus` and `swarm unfreeze` print it after their write, the ledger API serves it for every focus record at
``focus``, for one target at ``focus/<target>`` and for the tasks of one target in pages at
``focus/<target>/tasks``, and the focus unclaimed finding counts the ticks a focused, claimable task waits for a
seat.
"""

import sys
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass
from typing import IO, Any

from scripts.swarm import claim_hold, freeze
from scripts.swarm.store import SwarmError
from scripts.swarm_ledger.repository import hierarchy

CLAIMABLE = "claimable"
OUT_OF_SCOPE = "it is out of scope"
NO_TARGET = "no such target"
DONE = "it is already done"
STATES = {"pr": "in a pull request"}
BUSY = frozenset({"claimed", "pr"})
MINUTE_MS = 60_000
TASKS = "/tasks"


@dataclass(frozen=True)
class View:
    doc: dict[str, Any]
    graph: dict[str, Any]
    rows: dict[str, dict[str, Any]]
    covered: dict[str, list[dict[str, Any]]]
    fix: str


def view(doc: dict[str, Any], fix: str) -> View:
    """The ledger read once for every verdict: its plan graph, its tasks by id and by every node above them."""
    doc = {"tasks": [], **doc}
    graph: dict[str, Any] = hierarchy.project(doc)[0]  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    covered: dict[str, list[dict[str, Any]]] = {}
    for task in doc["tasks"]:
        for node in freeze.ancestry(task, graph):
            covered.setdefault(node, []).append(task)
    return View(doc, graph, {task["id"]: task for task in doc["tasks"]}, covered, fix)


def verdict(task: dict[str, Any], seen: View) -> str:
    """The claim gate's answer on one task: claimable, or the reason the tick may not claim it."""
    if task.get("out_of_scope"):
        return OUT_OF_SCOPE
    state = task.get("state") or "open"
    if state == "claimed":
        return f"it is claimed by {task.get('claimed_by') or 'an agent'}"
    if state != "open":
        return f"it is {STATES.get(state, state)}"
    return claim_hold.hold(task, seen.rows, seen.doc, seen.graph, seen.fix) or CLAIMABLE  # pyright: ignore[reportUnknownMemberType]


def known(name: str, seen: View) -> bool:
    """Whether a target names a plan node, a lane or kind selector, or a freeze or focus record."""
    selector, _, value = name.partition(":")
    records: list[dict[str, Any]] = seen.doc.get("freezes") or []
    return (
        bool(selector in freeze.SELECTORS and value)
        or name in seen.graph
        or any(record["target"] == name for record in records)
    )


def under(name: str, seen: View) -> list[dict[str, Any]]:
    """Every task a freeze or focus on the target covers."""
    selector, _, value = name.partition(":")
    if selector in freeze.SELECTORS and value:
        return [task for task in seen.doc["tasks"] if freeze.SELECTORS[selector](task) == value]
    return seen.covered.get(name, [])


def target(name: str, seen: View) -> dict[str, Any]:
    """The tasks under a target: the ids of those done and the claim gate verdict on every other one."""
    tasks = under(name, seen)
    rows = [
        {
            "id": task["id"],
            "lane": task.get("lane") or "",
            "state": task.get("state") or "open",
            "verdict": verdict(task, seen),
        }
        for task in tasks
        if task.get("state") != "done"
    ]
    return {
        "target": name,
        "claimable": sum(row["verdict"] == CLAIMABLE for row in rows),
        "done": [task["id"] for task in tasks if task.get("state") == "done"],
        "tasks": rows,
    }


def unclaimed(record: dict[str, Any], found: dict[str, Any], now_ms: int) -> int | None:
    """Minutes since the focus while a task under it is open and none is claimed or in a pull request."""
    states = {row["state"] for row in found["tasks"]}
    if "open" not in states or states & BUSY:
        return None
    return max((now_ms - record["at"]) // MINUTE_MS, 0)


def report(seen: View, now_ms: int) -> list[dict[str, Any]]:
    """Every focus record with the verdicts under its target and the minutes it has gone without a claim."""
    found: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = seen.doc.get("freezes") or []
    for record in records:
        if record["verb"] != "focus":
            continue
        answer = target(record["target"], seen)
        written = {key: record.get(key) for key in ("by", "at", "reason")}
        found.append({**answer, **written, "unclaimed_minutes": unclaimed(record, answer, now_ms)})
    return found


def read(doc: dict[str, Any], path: str, fix: str, now_ms: int) -> list[dict[str, Any]] | dict[str, Any] | None:
    """The ledger API's focus read: every focus record at ``focus``, one target's verdicts at ``focus/<target>``,
    and None for a target that does not exist."""
    seen, name = view(doc, fix), path.removeprefix("focus").removeprefix("/")
    if not name:
        return report(seen, now_ms)
    return target(name, seen) if known(name, seen) else None


def task_rows(doc: dict[str, Any], path: str, fix: str) -> list[dict[str, Any]] | None:
    """The ledger API's paged focus read: the tasks under the target of ``focus/<target>/tasks`` that are not done,
    each with its verdict. None when the path names no such list: it does not end in ``/tasks``, the whole name is
    itself a plan node, as for a task whose id is ``tasks``, or what stands before ``/tasks`` is no target."""
    name = path.removeprefix("focus").removeprefix("/")
    if not name.endswith(TASKS):
        return None
    seen, above = view(doc, fix), name.removesuffix(TASKS)
    if name in seen.graph or not known(above, seen):
        return None
    rows: list[dict[str, Any]] = target(above, seen)["tasks"]
    return rows


def lines(verb: str, name: str, found: dict[str, Any] | None) -> list[str]:
    """The verdict lines a focus or unfreeze prints: a head naming the claimable count, loud when it is zero."""
    if found is None:
        return [f"{verb} {name}: {NO_TARGET}"]
    counts = f"{len(found['tasks'])} open, {len(found['done'])} done"
    head = f"{verb} {name}: {found['claimable']} claimable, {counts}"
    if not found["claimable"]:
        head = f"{verb} {name}: NOTHING CLAIMABLE, no agent will start work from it; {counts}"
    rows = [f"  {row['id']}  {row['lane']}  {row['verdict']}" for row in found["tasks"]]
    return [head, *rows, *(f"  {task_id}  {DONE}" for task_id in found["done"])]


def answer(client: Any, slug: str, verb: str, name: str, out: IO[str]) -> None:
    """Write the claim gate verdict on every task under a focused or unfrozen target, or why the ledger gave none."""
    try:
        found = client.focus_verdicts(slug, name)
    except (SwarmError, urllib.error.HTTPError) as exc:
        out.write(f"{verb} {name}: written, but the ledger gave no verdict: {exc}\n")
        return
    for line in lines(verb, name, found):
        out.write(f"{line}\n")


def checked(command: Callable[[Any, Any], None], client: Callable[[], Any]) -> Callable[[Any, Any], None]:
    """The focus or unfreeze command, followed on standard error by the verdict on every task under its target."""

    def run(store: Any, args: Any) -> None:
        command(store, args)
        answer(client(), args.slug, args.command, args.target, sys.stderr)

    return run
