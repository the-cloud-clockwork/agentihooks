import json
import re
import threading
import tracemalloc
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

READERS = 20
HUNDRED = 100
TASKS = 2000
PEAK_TASKS = 500
SLUG = "ledger"
CURSOR = re.compile(r'"next_cursor": (?:"([^"]+)"|null)')


def document(tasks: int = TASKS) -> dict:
    from scripts.swarm_ledger.new_ledger import build_doc

    doc = build_doc({"title": "Task list memory"})
    doc["_meta"] = {"rev": 1, "updated_at": 1, "applied": []}
    doc["tasks"] = [
        {
            "id": f"t{index}",
            "title": f"Task {index}",
            "description": "Recorded request with é " * 12,
            "state": "open",
            "lane": "eng",
            "comments": [{"id": f"c{comment}", "text": "Recorded evidence " * 8} for comment in range(4)],
        }
        for index in range(tasks)
    ]
    doc["chat"] = [{"id": f"m{index}", "text": "Chat line from the swarm " * 8} for index in range(20000)]
    return doc


class Headers(dict):
    def get_content_type(self) -> str:
        return "application/json"


class Handler:
    command = "GET"

    def __init__(self, path: str) -> None:
        self.path = path
        self.headers = Headers({"Host": "h"})
        self.sent = None

    def exists(self, slug: str) -> bool:
        return slug == SLUG

    def send(self, status: int, body: str, ctype: str) -> None:
        self.sent = status, body


def services(repository: object) -> object:
    from scripts.swarm_ledger.api.services import HttpPolicy, Services

    core = SimpleNamespace(SLUG_RE=re.compile(r"[\w-]+"))
    authority = SimpleNamespace(caller=lambda headers, admin, slug: "agent", controller=lambda credential: False)
    return Services(
        repository=repository,
        ledger=SimpleNamespace(core=core, authority=authority),
        swarm=SimpleNamespace(),
        media=SimpleNamespace(),
        policy=HttpPolicy(allowed_hosts={"h"}, allowed_origins=(), max_body=1 << 10),
    )


def fetch(server: object, cursor: str | None) -> str:
    from scripts.swarm_ledger.api import routes

    query = f"limit=100&cursor={cursor}" if cursor else "limit=100"
    handler = Handler(f"/api/v1/ledgers/{SLUG}/tasks?{query}")
    routes.handle(handler, server)
    status, body = handler.sent
    assert status == 200, body[:200]
    return body


def walk(server: object) -> int:
    pages, cursor = 0, None
    while True:
        body = fetch(server, cursor)
        pages += 1
        cursor = CURSOR.search(body).group(1)
        if cursor is None:
            return pages


def store(tmp_path, tasks: int):
    from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository

    path = tmp_path / "ledger.sqlite3"
    SQLiteLedgerRepository(path).import_document(SLUG, document(tasks))
    return path


@pytest.fixture
def stored(tmp_path):
    return store(tmp_path, TASKS)


@pytest.fixture
def peak_stored(tmp_path):
    return store(tmp_path, PEAK_TASKS)


def test_full_task_list_walk_returns_every_task_in_order(stored):
    from scripts.swarm_ledger.api import resources
    from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository

    repository = SQLiteLedgerRepository(stored)
    server = services(repository)
    rows, cursor, expected = [], None, resources.read(repository.current(SLUG).state, "tasks", {"limit": 100})
    while True:
        reply = json.loads(fetch(server, cursor))
        if cursor is None:
            assert reply == expected
        rows.extend(reply["data"])
        cursor = reply["next_cursor"]
        if cursor is None:
            break
    assert [row["id"] for row in rows] == [f"t{index}" for index in range(TASKS)]
    assert all(row["comments_count"] == 4 and "comments" not in row for row in rows)


def test_concurrent_full_task_list_walks_return_the_same_pages(stored):
    from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository

    server = services(SQLiteLedgerRepository(stored))
    barrier, pages, failures = threading.Barrier(READERS), [], []

    def reader():
        try:
            barrier.wait()
            pages.append(walk(server))
        except Exception as exc:
            failures.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(READERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not failures, failures[0]
    assert len(pages) == READERS and len(set(pages)) == 1 and pages[0] > 1


def test_hundred_concurrent_full_task_list_reads_stay_under_a_fixed_peak(peak_stored):
    from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository

    tracemalloc.start()
    try:
        SQLiteLedgerRepository(peak_stored).current(SLUG)
        _, whole = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        SQLiteLedgerRepository(peak_stored).read(SLUG, "tasks")
        _, tasks = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    server = services(SQLiteLedgerRepository(peak_stored))
    barrier, pages, failures = threading.Barrier(HUNDRED), [], []

    def reader():
        try:
            barrier.wait()
            pages.append(walk(server))
        except Exception as exc:
            failures.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(HUNDRED)]
    tracemalloc.start()
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert not failures, failures[0]
    assert len(pages) == HUNDRED and len(set(pages)) == 1 and pages[0] > 1
    report = (
        f"{HUNDRED} task list walks peaked at {peak} bytes; one load peaks at {whole} whole, {tasks} task rows only"
    )
    assert peak < whole, report
    assert peak < tasks * 1.5, report
