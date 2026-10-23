from dataclasses import dataclass

from scripts.events.envelope import Envelope


@dataclass(frozen=True)
class EventType:
    entity: str
    fields: frozenset[str]


TASK_FIELDS = frozenset(
    ("id", "title", "description", "phase", "lane", "state", "claimed_by", "issue_url", "pr_url", "done")
)
PHASE_FIELDS = frozenset(("id", "title", "description", "done"))

LEDGER = {
    "task.changed": EventType("task", TASK_FIELDS),
    "task.removed": EventType("task", frozenset()),
    "phase.changed": EventType("phase", PHASE_FIELDS),
    "phase.removed": EventType("phase", frozenset()),
}


def refusal(event: Envelope) -> str | None:
    kind = LEDGER.get(event.type)
    if kind is None:
        return f"unknown ledger event type {event.type!r}"
    if event.entity != kind.entity:
        return f"{event.type} is a {kind.entity} event, not {event.entity}"
    if missing := sorted(kind.fields - event.data.keys()):
        return f"{event.type} data lacks {' '.join(missing)}"
    if not kind.fields and event.data:
        return f"{event.type} carries no data"
    return None
