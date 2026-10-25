import json
import threading

from . import resources

PAGES_KEPT = 32
ENCODER = json.JSONEncoder(ensure_ascii=False)
LEDGERS_KEPT = 8


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


class Tasks:
    def __init__(self, repository: object, version: tuple) -> None:
        self.repository, self.version, self.lock = repository, version, threading.Lock()
        self.revision, self.rows, self.pages = None, None, {}

    def load(self, slug: str) -> None:
        state = self.repository.read(slug, "tasks", "_meta.task_revision", "_meta.rev")
        self.revision = resources.resource_revision(state, "tasks")
        self.rows = state.get("tasks") or []

    def page(self, query: dict) -> Page:
        key = (query.get("cursor"), query.get("limit"))
        if key not in self.pages:
            if len(self.pages) >= PAGES_KEPT:
                self.pages.clear()
            self.pages[key] = Page(resources.page(self.rows, self.revision, query, hash_page=True))
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
