import json
import threading

from . import resources

PAGES_KEPT = 32
LEDGERS_KEPT = 8


class Page(dict):
    def __init__(self, reply: dict) -> None:
        super().__init__(reply)
        self._lock = threading.Lock()
        self._text = None

    def text(self) -> str:
        with self._lock:
            if self._text is None:
                self._text = json.dumps(self, ensure_ascii=False)
            return self._text


class Tasks:
    def __init__(self, repository: object, version: tuple) -> None:
        self.repository, self.version, self.lock = repository, version, threading.Lock()
        self.revision, self.rows, self.revisions, self.pages = None, None, None, {}

    def load(self, slug: str) -> None:
        state = self.repository.read(slug, "tasks", "_meta.task_revision", "_meta.rev")
        self.revision = resources.resource_revision(state, "tasks")
        self.rows = state.get("tasks") or []
        self.revisions = resources.collection_revisions(self.rows)[1]

    def page(self, query: dict) -> Page:
        key = (query.get("cursor"), query.get("limit"))
        if key not in self.pages:
            if len(self.pages) >= PAGES_KEPT:
                self.pages.clear()
            reply = resources.page(self.rows, self.revision, query, self.revisions, True)
            self.pages[key] = Page(reply)
        return self.pages[key]


HELD: dict = {}
GUARD = threading.Lock()


def read(repository: object, slug: str, query: dict) -> Page:
    version = repository.task_version(slug)
    with GUARD:
        held = HELD.get(slug)
        if held is None or held.repository is not repository or held.version != version:
            held = Tasks(repository, version)
        HELD.pop(slug, None)
        HELD[slug] = held
        while len(HELD) > LEDGERS_KEPT:
            HELD.pop(next(iter(HELD)))
    with held.lock:
        if held.rows is None:
            held.load(slug)
        return held.page(query)
