import json
import threading
from collections.abc import Mapping, Sequence
from typing import Any

from . import resources

PAGES_KEPT = 32
ENCODER = json.JSONEncoder(ensure_ascii=False)
LEDGERS_KEPT = 4
SELECTORS = ("ids", "states", "pinned", "branched")
SELECTION = (*SELECTORS, "fields")
SELECTION_ONLY = "Only a plain tasks read takes a selection, and it names ids, states, pinned or branched"


class Page(dict):
    def __init__(self, reply: dict) -> None:
        super().__init__(reply)
        self._lock = threading.Lock()
        self._text = None

    def text(self) -> str:
        with self._lock:
            if self._text is None:
                self._text = ENCODER.encode(self)
            return self._text


def row_revision(row: object) -> str | None:
    return resources.revision(row) if isinstance(row, dict) else None


class Tasks:
    def __init__(self, repository: object) -> None:
        self.repository, self.lock = repository, threading.Lock()
        self.stored: tuple[Any, ...] | None = None
        self.revision, self.revisions, self.pages = None, [], {}

    def refresh(self, slug: str) -> None:
        stored = self.repository.task_rows(slug, self.stored)
        if stored is self.stored:
            return
        rows, revision = stored[2], resources.resource_revision({"_meta": stored[1]}, "tasks")
        if self.stored is None or rows is not self.stored[2]:
            known = {} if self.stored is None else dict(zip(map(id, self.stored[2]), self.revisions))
            self.revisions = [known.get(id(row)) or row_revision(row) for row in rows]
            self.pages = {}
        self.stored, self.revision = stored, revision

    def page(self, query: dict) -> Page:
        key = (query.get("cursor"), query.get("limit"))
        if key not in self.pages:
            if len(self.pages) >= PAGES_KEPT:
                self.pages.clear()
            self.pages[key] = Page(resources.page(self.stored[2], self.revision, query, self.revisions, True))
        return self.pages[key]


HELD: dict = {}
GUARD = threading.Lock()


def read(repository: object, slug: str, query: dict[str, Any]) -> dict[str, Any]:
    with GUARD:
        held = HELD.pop(slug, None)
        if held is None or held.repository is not repository:
            held = Tasks(repository)
        HELD[slug] = held
        while len(HELD) > LEDGERS_KEPT:
            HELD.pop(next(iter(HELD)))
    with held.lock:
        held.refresh(slug)
        if "targets" in query:
            return targeting(held.stored[2], query["targets"])
        if query.keys() & set(SELECTION):
            return selecting(held.stored[2], query)  # pyright: ignore[reportUnknownMemberType, reportOptionalSubscript, reportUnknownArgumentType]
        return held.page(query)


def targeting(rows: list[Any], url: str) -> dict[str, Any]:
    """The tasks whose pull request, split halves or pull scopes name this pull request URL, in one reply."""
    found: list[dict[str, Any]] = [row for row in rows if isinstance(row, dict) and url in pull_links(row)]
    data = [resources.project({**row, "revision": resources.revision(row)}) for row in found]
    return resources.bounded({"data": data, "revision": resources.revision(found), "next_cursor": None})


def selecting(rows: Sequence[Mapping[str, Any]], query: dict[str, Any]) -> dict[str, Any]:
    """The task rows matching every filter the query names: ids, states, a hive or workstation pin and a branch with
    a pull request, each without its threads or cut to the named fields beside its id, in one reply."""
    ids, states, fields = _listed(query.get("ids")), _listed(query.get("states")), _listed(query.get("fields"))
    found = [
        row
        for row in rows
        if isinstance(row, dict)
        and (not ids or row.get("id") in ids)
        and (not states or row.get("state") in states)
        and ("pinned" not in query or bool(row.get("hive") or row.get("workstation")))
        and ("branched" not in query or bool(row.get("branch") and row.get("pr_url")))
    ]
    reply = {"data": [_shown(row, fields) for row in found], "revision": resources.revision(found), "next_cursor": None}
    bounded: dict[str, Any] = resources.bounded(reply)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return bounded


def selection(path: str, query: dict[str, Any]) -> bool:
    """Whether a query is a narrow selection of the tasks collection alone, naming at least one filter."""
    return path == "tasks" and query.keys() <= set(SELECTION) and any(query.get(key) for key in SELECTORS)


def _listed(value: object) -> frozenset[str]:
    return frozenset(filter(None, str(value or "").split(",")))


def _shown(row: dict[str, Any], fields: frozenset[str]) -> dict[str, Any]:
    if fields:
        return {key: row[key] for key in ("id", *sorted(fields)) if key in row}
    shown: dict[str, Any] = resources.project({**row, "revision": resources.revision(row)})  # pyright: ignore[reportAssignmentType]
    return shown


def pull_links(row: dict[str, Any]) -> set[Any]:
    """Every pull request URL a task row names."""
    return {row.get("pr_url"), *(row.get("split_pulls") or []), *(row.get("pull_scopes") or {})}
