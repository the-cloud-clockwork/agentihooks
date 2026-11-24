from dataclasses import fields
from typing import Any, Protocol, TypeGuard, cast

from scripts.swarm_ledger import ledger_closed, models, ports
from scripts.swarm_ledger.repository import hierarchy


class Graph(Protocol):
    def project(self, state: dict[str, Any], /) -> tuple[dict[str, Any], set[str]]: ...


class Closing(Protocol):
    def trimmed(self, state: dict[str, Any], /) -> tuple[dict[str, Any], dict[str, Any]]: ...


GRAPH = cast(Graph, hierarchy)
CLOSING = cast(Closing, ledger_closed)

PAGED = ("tasks", "phases", "followups", "alerts", "notifications", "slices", "freezes")
READS = (
    *(field.name for field in fields(models.Ledger) if field.name != "tasks"),
    "time_left_minutes",
    "closed_at",
    "policy",
    "summary",
    "size",
    "_meta",
)
META_DROPPED = ("seeds", "api_operations", "members", "events")
EVENTS_KEPT = 300
TASK_STATES = {"pr": 0, "claimed": 1, "open": 2, "blocked": 3}
RANKS = ("urgent", "high", "normal", "low")


def build(parts: dict[str, Any], tasks: list[Any], holds: tuple[str, bool], limits: dict[str, Any]) -> dict[str, Any]:
    state = {**parts, "tasks": tasks}
    swarm = ports.swarm()
    marks = swarm.freeze.marks(state, GRAPH.project(state)[0], *holds)
    status = swarm.phase_state.status(state)
    closed = CLOSING.trimmed(state)[1]
    raw: dict[str, Any] = parts.get("_meta") or {}
    meta = {key: value for key, value in raw.items() if key not in META_DROPPED}
    meta.update(closed=closed, limits=limits, events=(raw.get("events") or [])[-EVENTS_KEPT:])
    kept = {key: value for key, value in parts.items() if key not in PAGED and key != "_meta"}
    sections = {name: section(name, state.get(name) or []) for name in PAGED}
    return {**kept, "marks": marks, "phase_status": status, "sections": sections, "_meta": meta}


def open_rows(name: str, rows: list[Any]) -> list[tuple[int, dict[str, Any]]]:
    return [
        (at, row) for at, row in enumerate(rows) if is_row(row) and not row.get("deleted") and not closed(name, row)
    ]


def is_row(value: object) -> TypeGuard[dict[str, Any]]:
    return isinstance(value, dict)


def view_rows(name: str, rows: list[Any], frozen: set[str]) -> list[tuple[int, dict[str, Any]]]:
    shown = [(at, row) for at, row in enumerate(rows) if is_row(row)]
    ended = [at for at, row in shown if closed(name, row)]
    dropped = set(ended[: len(ended) - ledger_closed.KEPT])
    kept = [(at, row) for at, row in shown if at not in dropped]
    if name != "tasks":
        return kept
    ending = [(at, row) for at, row in kept if closed(name, row)]
    return ending + page_order([(at, row) for at, row in kept if not closed(name, row)], frozen)


def closed(name: str, row: dict[str, Any]) -> bool:
    return name in ledger_closed.COLLECTIONS and ledger_closed.closed(name, row)


def state_of(name: str, row: dict[str, Any]) -> str:
    if row.get("out_of_scope"):
        return "out"
    return (row.get("state") or "open") if name == "tasks" else "open"


def section(name: str, rows: list[Any]) -> dict[str, Any]:
    states: dict[str, int] = {}
    found = open_rows(name, rows)
    for _, row in found:
        state = state_of(name, row)
        states[state] = states.get(state, 0) + 1
    return {"open": len(found), "states": states}


def page_order(rows: list[tuple[int, dict[str, Any]]], frozen: set[str]) -> list[tuple[int, dict[str, Any]]]:
    def key(pair: tuple[int, dict[str, Any]]) -> tuple[bool, int, int, int]:
        at, row = pair
        rank = row.get("rank") if row.get("rank") in RANKS else "normal"
        held = f"tasks/{row.get('id')}" in frozen
        return held, TASK_STATES.get(row.get("state") or "open", len(TASK_STATES)), RANKS.index(rank), at

    return sorted(rows, key=key)
