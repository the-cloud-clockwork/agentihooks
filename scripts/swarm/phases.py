"""Phase checkboxes kept by the minute tick: a phase with tasks is done exactly when every one of them is done.

A phase without tasks is left to the master. Each change carries a status comment and an item for the master.
"""

from dataclasses import dataclass
from typing import Any

from scripts.swarm.ledger_events import SENDER, Mail
from scripts.swarm_ledger.repository import hierarchy

TICKED = "Every task in this phase is done, so the swarm ticked it."
REOPENED = "A task that is not done landed in this phase, so the swarm reopened it."


@dataclass(frozen=True)
class PhaseChange:
    """A phase the phase pass ticks or reopens: its id and title, its new done state, its task count and open tasks."""

    phase_id: str
    title: str
    done: bool
    count: int
    open_ids: tuple[str, ...]


def phase_changes(slug: str, doc: dict[str, Any], ledger: Any) -> list[str | PhaseChange]:
    """Return each phase whose tasks call for a tick or a reopen, writing nothing, from the pass document alone."""
    under = projected_phase_tasks(doc)
    return [change for phase in doc.get("phases", []) if (change := _change(phase, under)) is not None]


def _change(phase: dict[str, Any], under: dict[str, list[tuple[str, str]]]) -> PhaseChange | None:
    """Return the change ``phase`` needs from the states of its tasks, or nothing when its checkbox already agrees."""
    mine = [(tid, state) for tid, state in under.get(phase["id"], []) if state != "out_of_scope"]
    if phase.get("out_of_scope") or not mine:
        return None
    open_ids = tuple(tid for tid, state in mine if state != "done")
    done = not open_ids
    if done == bool(phase.get("done")):
        return None
    return PhaseChange(phase["id"], phase["title"], done, len(mine), open_ids)


def apply_change(inbox: Any, store: Any, slug: str, ledger: Any, change: PhaseChange) -> str:
    """Set the checkbox ``change`` names, tell the master and return the action line."""
    ledger.set_phase(slug, change.phase_id, change.done, TICKED if change.done else REOPENED)
    if change.done:
        text = f"For your information: phase {change.phase_id} {change.title} is done, all {change.count} tasks closed."
    else:
        text = (
            f"For your information: phase {change.phase_id} {change.title} reopened for {', '.join(change.open_ids)}."
        )
    mail = Mail(inbox, store, slug)
    mail.inbox.send(SENDER, mail.master, text, fyi=True)
    return f"phase {change.phase_id} {'ticked' if change.done else 'reopened'}"


def _task_state(task: dict[str, Any]) -> str:
    """The state the ledger hierarchy reports for ``task``."""
    if task.get("out_of_scope"):
        return "out_of_scope"
    return task.get("state") or ("done" if task.get("done") else "open")


def _phase_of(node: str | None, nodes: dict[str, tuple[str, str | None, int]]) -> str | None:
    """The phase node above ``node`` in the projected hierarchy, or nothing when no phase holds it."""
    seen: set[str] = set()
    while node is not None and node in nodes and node not in seen:
        if nodes[node][0] == "phase":
            return node
        seen.add(node)
        node = nodes[node][1]
    return None


def projected_phase_tasks(doc: dict[str, Any]) -> dict[str, list[tuple[str, str]]]:
    """Each phase's tasks with the states the ledger hierarchy reports, projected from ``doc`` with no ledger read."""
    nodes, _ = hierarchy.project(doc)
    found: dict[str, list[tuple[str, str]]] = {
        node.removeprefix("phases/"): [] for node, row in nodes.items() if row[0] == "phase"
    }
    for task in doc.get("tasks") or []:
        node = f"tasks/{task.get('id')}"
        if node in nodes and (phase := _phase_of(nodes[node][1], nodes)) is not None:
            found[phase.removeprefix("phases/")].append((task["id"], _task_state(task)))
    return found
