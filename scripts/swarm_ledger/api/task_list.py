import json
import threading
from typing import Any

from . import resources

PAGES_KEPT = 32
ENCODER = json.JSONEncoder(ensure_ascii=False)
LEDGERS_KEPT = 4


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
        return held.page(query)


def targeting(rows: list[Any], url: str) -> dict[str, Any]:
    """The tasks whose pull request, split halves or pull scopes name this pull request URL, in one reply."""
    found: list[dict[str, Any]] = [row for row in rows if isinstance(row, dict) and url in pull_links(row)]
    data = [resources.project({**row, "revision": resources.revision(row)}) for row in found]
    return resources.bounded({"data": data, "revision": resources.revision(found), "next_cursor": None})


def pull_links(row: dict[str, Any]) -> set[Any]:
    """Every pull request URL a task row names."""
    return {row.get("pr_url"), *(row.get("split_pulls") or []), *(row.get("pull_scopes") or {})}
