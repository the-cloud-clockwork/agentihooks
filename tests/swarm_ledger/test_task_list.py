import re
import time

import pytest

pytestmark = pytest.mark.unit


def ledger(revision: int, *titles: str) -> dict:
    tasks = [{"id": f"t{index}", "title": title, "comments": [{"id": "c"}]} for index, title in enumerate(titles)]
    return {"tasks": tasks, "_meta": {"task_revision": revision, "rev": revision + 10}}


@pytest.mark.parametrize("query", [{"state": "done"}, {"offset": 0}, {"q": "Done"}])
def test_closed_task_queries_read_the_stored_entry(query):
    from types import SimpleNamespace

    from scripts.swarm_ledger.api import resources, routes

    doc = ledger(9, "Open")
    entry = SimpleNamespace(state=doc, views={})
    host = SimpleNamespace(repository=SimpleNamespace(current=lambda slug: entry))
    try:
        expected = resources.read(doc, "tasks", query)
    except resources.APIError as error:
        with pytest.raises(resources.APIError, match=re.escape(str(error))):
            routes.ledger_read(host, "closed", "tasks", query)
    else:
        assert routes.ledger_read(host, "closed", "tasks", query) == expected


def test_a_task_list_read_after_a_replace_returns_the_new_tasks(tmp_path):
    from types import SimpleNamespace

    from scripts.swarm_ledger.api import routes
    from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository

    repository = SQLiteLedgerRepository(tmp_path / "ledger.sqlite3")
    host = SimpleNamespace(repository=repository)
    repository.import_document("again", {"title": "Again", **ledger(2, "Before")})
    assert routes.ledger_read(host, "again", "tasks", {})["data"][0]["title"] == "Before"
    time.sleep(0.002)
    repository.import_document("again", {"title": "Again", **ledger(2, "After")}, replace=True)
    assert routes.ledger_read(host, "again", "tasks", {})["data"][0]["title"] == "After"


def test_a_write_to_one_task_hashes_only_that_task_on_the_next_list_read(tmp_path, monkeypatch):
    import hashlib
    from types import SimpleNamespace

    from scripts.swarm_ledger.api import resources, routes
    from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository

    path = tmp_path / "ledger.sqlite3"
    SQLiteLedgerRepository(path).import_document("one", {"title": "One", **ledger(2, *(f"T{n}" for n in range(200)))})
    reader = SimpleNamespace(repository=SQLiteLedgerRepository(path))
    before = routes.ledger_read(reader, "one", "tasks", {"limit": 5})
    SQLiteLedgerRepository(path).apply_ops("one", changes=[{"path": "tasks/t150/title", "value": "Changed"}])
    hashes = []
    counting = SimpleNamespace(sha256=lambda *data: hashes.append(data) or hashlib.sha256(*data))
    monkeypatch.setattr(resources, "hashlib", counting)
    after = routes.ledger_read(reader, "one", "tasks", {"limit": 200})
    counted = len(hashes)
    stored = SQLiteLedgerRepository(path).read("one", "tasks/t150")["tasks"][0]
    assert after["revision"] != before["revision"]
    assert after["data"][150]["title"] == "Changed"
    assert after["data"][150]["revision"] == resources.revision(stored)
    assert after["data"][149]["revision"] == resources.revision(ledger(2, *(f"T{n}" for n in range(200)))["tasks"][149])
    assert counted <= 3, counted


def test_concurrent_identical_task_list_reads_share_one_result(tmp_path):
    import threading
    from types import SimpleNamespace

    from scripts.swarm_ledger.api import routes
    from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository

    path = tmp_path / "ledger.sqlite3"
    SQLiteLedgerRepository(path).import_document("same", {"title": "Same", **ledger(2, *(f"T{n}" for n in range(50)))})
    host = SimpleNamespace(repository=SQLiteLedgerRepository(path))
    barrier, replies = threading.Barrier(8), []

    def reader():
        barrier.wait()
        replies.append(routes.ledger_read(host, "same", "tasks", {"limit": 20}))

    threads = [threading.Thread(target=reader) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(replies) == 8 and all(reply is replies[0] for reply in replies)
