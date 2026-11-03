"""Phase checkboxes kept by the minute tick: a phase with tasks is done exactly when every one of them is done.

A phase without tasks is left to the master. Each change carries a status comment and an item for the master.
"""

from dataclasses import dataclass
from typing import Any

from scripts.swarm.ledger_events import SENDER, Mail

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
    """Return each phase whose tasks call for a tick or a reopen, writing nothing; a failed hierarchy read waits a tick."""
    try:
        under = phase_tasks(ledger.hierarchy(slug))
    except OSError as exc:
        return [f"phase pass skipped, the hierarchy read failed: {exc}"]
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
    inbox.send(SENDER, Mail(inbox, store, slug).master, text, fyi=True)
    return f"phase {change.phase_id} {'ticked' if change.done else 'reopened'}"


def phase_tasks(rows: list) -> dict:
    found, phase = {}, None
    for row in rows:
        if phase is not None and row["depth"] <= phase[1]:
            phase = None
        if row["kind"] == "phase":
            phase = (row["node"].removeprefix("phases/"), row["depth"])
            found[phase[0]] = []
        elif row["kind"] == "task" and phase is not None:
            found[phase[0]].append((row["node"].removeprefix("tasks/"), row["state"]))
    return found
