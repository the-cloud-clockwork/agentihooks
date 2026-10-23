from dataclasses import dataclass

from scripts.events.envelope import Envelope


@dataclass(frozen=True)
class EventType:
    entity: str
    fields: frozenset[str]


TASK_DEFAULTS = {
    "title": "",
    "description": "",
    "phase": "",
    "lane": "eng",
    "state": "open",
    "claimed_by": "",
    "issue_url": "",
    "pr_url": "",
    "done": False,
}
PHASE_DEFAULTS = {"title": "", "description": "", "done": False}
TASK_FIELDS = frozenset(("id", *TASK_DEFAULTS))
PHASE_FIELDS = frozenset(("id", *PHASE_DEFAULTS))
DEFAULTS = {"tasks": TASK_DEFAULTS, "phases": PHASE_DEFAULTS}

# Fields every ledger write path builds and every import fills.
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


def filled(state: dict) -> dict:
    """The document with every task and phase carrying each catalogued field; present values are kept."""
    return {
        **state,
        **{
            collection: [{**defaults, **item} if isinstance(item, dict) else item for item in state[collection]]
            for collection, defaults in DEFAULTS.items()
            if isinstance(state.get(collection), list)
        },
    }
