"""A claim shaped task update written on its task's own stored rows."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

import ledger_priorities  # pyright: ignore[reportMissingImports]

from scripts.swarm_ledger import request_metrics

from . import mutation, outbox, refresh
from .entry import thawed
from .events import append_events, read_events, trim_events  # pyright: ignore[reportUnknownVariableType]
from .rows import TABLES, assemble, diff, encode, write_rows  # pyright: ignore[reportUnknownVariableType]
from .sqlite import (
    CHANGES_KEPT,
    GENERATION,
    ROOT,
    UPDATE,
    Missing,
    partial_rows,  # pyright: ignore[reportUnknownVariableType]
    scope,  # pyright: ignore[reportUnknownVariableType]
    without_events,  # pyright: ignore[reportUnknownVariableType]
)

Doc = dict[str, Any]
Row = tuple[Any, ...]
Rows = dict[str, Row]
Parts = tuple[list[Any], ...]
META = (
    "rev",
    "task_revision",
    "updated_at",
    "warnings",
    "api_operations",
    "events_ack",
    "events_trimmed",
    "created_at",
)
STAMPED = ("claim_generation", "repository_branches")
REQUIRED = (
    ("alerts",),
    ("priorities",),
    ("notifications",),
    ("_meta", "members"),
    ("_meta", "events"),
    ("_meta", "priorities_dismissed"),
)
UNSETTLED = ("done", "blocked")
SUMMARY = "SELECT summary FROM ledgers WHERE slug=?"
EVENTS: str = encode(["_meta", "events"])
ROWS = "SELECT path, parent, key, position, kind, value FROM {table} WHERE slug=? AND path IN ({marks})"
REPLACED = "narrow task write replaced a partly read container"


@dataclass(frozen=True)
class Written:
    """A narrow write's answer: the touched task row as stored, the rejected op ids and the partial state it wrote."""

    task: Doc
    rejected: list[str]
    state: Doc


def task_parts(task_id: str) -> list[Any]:
    return ["tasks", ["id", task_id, 0]]


def keys(task_id: str, fields: Doc) -> Parts:
    stamps = [["_meta", "stamps", f"tasks/{task_id}/{field}"] for field in (*fields, *STAMPED)]
    return (
        task_parts(task_id),
        ["alerts"],
        ["size"],
        *(["_meta", key] for key in META),
        ["_meta", "outcomes", task_id],
        *stamps,
    )


def stored_at(connection: Any, slug: str, paths: list[str]) -> Rows:
    found: Rows = {}
    if not paths:
        return found
    marks = ",".join("?" * len(paths))
    for table in TABLES:
        for path, parent, key, position, kind, value in connection.execute(
            ROWS.format(table=table, marks=marks), (slug, *paths)
        ):
            found[path] = (table, parent, key, position, kind, value)
    return found


def children(connection: Any, slug: str, parent: str) -> int:
    held: list[Row] = refresh.child_rows(connection, slug, parent)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return sum(1 for row in held if row[0] != EVENTS)


def row_of(state: Doc, task_id: str) -> Doc | None:
    tasks: list[Doc] = state.get("tasks") or []
    return next((task for task in tasks if task.get("id") == task_id), None)


def lines(task: Doc) -> Doc:
    wanted = cast(Callable[[Doc], Doc], ledger_priorities.wanted)  # pyright: ignore[reportUnknownMemberType]
    return wanted({"tasks": [task], "phases": []})


def admitted(connection: Any, slug: str, state: Doc, task: Doc, op: Doc) -> bool:
    meta: Doc = state["_meta"]
    if task.get("done") is True or task.get("state", "open") in UNSETTLED or not meta.get("created_at"):
        return False
    if "stamps" not in meta or lines(task) != lines({**task, **op["fields"]}):
        return False
    required = [encode(list(parts)) for parts in REQUIRED]
    return len(stored_at(connection, slug, required)) == len(required)


def partial_containers(parts: Parts) -> set[str]:
    read = {encode(key) for key in parts}
    found: set[str] = set()
    for key in parts:
        ancestors: list[str] = scope(key)[0]  # pyright: ignore[reportUnknownVariableType]
        found.update(path for path in ancestors if path not in read)
    return found


def placed(connection: Any, slug: str, rows: Rows, after: Rows, partial: set[str]) -> Rows:
    if any(path in partial for path in after):
        raise RuntimeError(REPLACED)
    known = {**stored_at(connection, slug, [path for path in after if path not in rows]), **rows}
    counts: dict[str, int] = {}
    found: Rows = {}
    for path, row in after.items():
        table, parent, key, position, kind, value = row
        if parent in partial:
            if path in known:
                key, position = known[path][2], known[path][3]
            else:
                if parent not in counts:
                    counts[parent] = children(connection, slug, parent)
                position = counts[parent]
                counts[parent] += 1
        found[path] = (table, parent, key, position, kind, value)
    return found


def read(connection: Any, slug: str, parts: Parts) -> Rows:
    rows: Rows = partial_rows(connection, slug, parts)  # pyright: ignore[reportUnknownVariableType]
    return rows


def update(repository: Any, connection: Any, slug: str, op: Doc, gate: Any) -> Written | None:
    task_id: str = op["item"].split("/")[1]
    parts = keys(task_id, op["fields"])
    rows = read(connection, slug, parts)
    if ROOT not in rows:
        raise Missing(slug)
    state = cast(Doc, assemble(rows))
    task = row_of(state, task_id)
    if task is None or not admitted(connection, slug, state, task, op):
        return None
    dependencies: list[str] = [*(task.get("depends_on") or []), *(task.get("parked_on") or [])]
    named = tuple(task_parts(name) for name in dependencies)
    if named:
        parts = (*parts, *named)
        rows.update(read(connection, slug, named))
        state = cast(Doc, assemble(rows))
    state["_meta"]["events"] = read_events(connection, slug)
    before = cast(Doc, thawed(state))
    rejected, ctx = mutation.narrow(slug, state, repository.domain, op, gate)
    persist(repository, connection, slug, (rows, partial_containers(parts)), (before, state), ctx)
    written = row_of(state, task_id)
    return Written({} if written is None else written, rejected, state)


def persist(
    repository: Any, connection: Any, slug: str, stored_rows: tuple[Rows, set[str]], states: tuple[Doc, Doc], ctx: Any
) -> None:
    rows, partial = stored_rows
    before, state = states
    meta: Doc = state["_meta"]
    previous: Doc = before["_meta"]
    meta["task_revision"] = (
        meta["rev"] if state["tasks"] != before["tasks"] else previous.get("task_revision", previous["rev"])
    )
    repository.admit(connection, state)
    unlogged: tuple[Doc, Doc] = (without_events(before), without_events(state))  # pyright: ignore[reportUnknownVariableType]
    old, new = cast(tuple[Rows, Rows], diff(*unlogged))
    after = placed(connection, slug, rows, new, partial)
    stored = {**stored_at(connection, slug, [path for path in old if path not in rows]), **rows}
    paths = cast(list[str], write_rows(connection, slug, {path: stored[path] for path in old if path in stored}, after))
    events: list[Doc] = ctx.events if ctx.changed else []
    kept = len(meta["events"])
    with request_metrics.step("event_append"):
        append_events(connection, slug, events, kept)
        if not events:
            trim_events(connection, slug, kept)
    with request_metrics.step("outbox_write"):
        outbox.write(connection, slug, meta["rev"], before, state)  # pyright: ignore[reportUnknownMemberType]
    generation: int = connection.execute(GENERATION, (slug,)).fetchone()[0] + 1
    with request_metrics.step("refresh_record"):
        refresh.record(connection, slug, generation, paths, CHANGES_KEPT)  # pyright: ignore[reportUnknownMemberType]
    summary: Doc = {
        **json.loads(connection.execute(SUMMARY, (slug,)).fetchone()[0]),
        "updated_at": meta.get("updated_at"),
    }
    connection.execute(UPDATE, (meta["rev"], generation, encode(summary), repository.domain.now_ms(), slug))
