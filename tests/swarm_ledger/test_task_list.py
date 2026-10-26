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
