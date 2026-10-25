import json
import re
import time

import pytest

pytestmark = pytest.mark.unit


class Repository:
    def __init__(self, state: dict) -> None:
        self.state, self.reads = state, 0

    def task_version(self, slug: str) -> tuple[int, int]:
        return self.state["_meta"]["task_revision"], 0

    def read(self, slug: str, *keys: str) -> dict:
        assert keys == ("tasks", "_meta.task_revision", "_meta.rev")
        self.reads += 1
        return {key: value for key, value in self.state.items() if key in ("tasks", "_meta")}


def ledger(revision: int, *titles: str) -> dict:
    tasks = [{"id": f"t{index}", "title": title, "comments": [{"id": "c"}]} for index, title in enumerate(titles)]
    return {"tasks": tasks, "_meta": {"task_revision": revision, "rev": revision + 10}}


def test_identical_reads_share_one_page_and_one_task_load():
    from scripts.swarm_ledger.api import resources, task_list

    repository = Repository(ledger(7, "Café", "Second"))
    first = task_list.read(repository, "shared", {"limit": 1})
    assert task_list.read(repository, "shared", {"limit": 1}) is first
    assert repository.reads == 1
    assert first == resources.read({**repository.state, "_meta": {"rev": 7}}, "tasks", {"limit": 1})
    assert first["revision"] == f"{7:064x}"
    assert first["page_hash"] == resources.revision(first["data"])
    assert first["data"][0]["revision"] == resources.revision(repository.state["tasks"][0])
    assert first["data"][0]["comments_count"] == 1
    text = first.text()
    assert first.text() is text
    assert "Café" in text
    assert json.loads(text) == first


def test_queries_get_their_own_pages_from_the_loaded_rows():
    from scripts.swarm_ledger.api import task_list

    repository = Repository(ledger(1, "First", "Second"))
    first = task_list.read(repository, "queries", {"limit": 1})
    rest = task_list.read(repository, "queries", {"limit": 1, "cursor": first["next_cursor"]})
    whole = task_list.read(repository, "queries", {"limit": 2})
    assert [row["id"] for row in first["data"] + rest["data"]] == ["t0", "t1"]
    assert [row["id"] for row in whole["data"]] == ["t0", "t1"]
    assert rest["next_cursor"] is None and whole["next_cursor"] is None
    assert repository.reads == 1


def test_a_new_task_revision_or_repository_loads_the_rows_again():
    from scripts.swarm_ledger.api import resources, task_list

    repository = Repository(ledger(1, "Before"))
    assert task_list.read(repository, "moved", {"limit": 1})["data"][0]["title"] == "Before"
    repository.state = ledger(2, "After")
    after = task_list.read(repository, "moved", {"limit": 1})
    assert after["data"][0]["title"] == "After"
    assert after["revision"] == f"{2:064x}"
    with pytest.raises(resources.APIError, match="Collection changed"):
        task_list.read(repository, "moved", {"limit": 1, "cursor": f"{1:064x}:1"})
    other = Repository(ledger(2, "Other"))
    assert task_list.read(other, "moved", {"limit": 1})["data"][0]["title"] == "Other"
    assert (repository.reads, other.reads) == (2, 1)


def test_a_ledger_without_tasks_pages_an_empty_collection():
    from scripts.swarm_ledger.api import task_list

    repository = Repository({"_meta": {"task_revision": 3}})
    reply = task_list.read(repository, "empty", {})
    assert reply["data"] == [] and reply["next_cursor"] is None


def test_the_task_route_reads_task_rows_without_the_stored_entry():
    from types import SimpleNamespace

    from scripts.swarm_ledger.api import routes, task_list

    repository = Repository(ledger(8, "Routed"))
    repository.current = None
    reply = routes.ledger_read(SimpleNamespace(repository=repository), "routed", "tasks", {"limit": 1})
    assert isinstance(reply, task_list.Page)
    assert reply["data"][0]["title"] == "Routed"


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


def test_a_shared_page_is_sent_as_its_encoded_text(monkeypatch):
    from types import SimpleNamespace

    from scripts.swarm_ledger.api import routes, task_list

    page = task_list.Page({"data": [{"title": "Café"}], "revision": "r", "next_cursor": None})
    sent = []
    handler = SimpleNamespace(path="/api/v1/ledgers/sent/tasks", send=lambda *args: sent.append(args))
    monkeypatch.setattr(routes, "dispatch", lambda handler, services: page)
    routes.handle(handler, None)
    assert sent == [(200, page.text(), "application/json")]
    assert '"Café"' in sent[0][1]


def test_pages_kept_per_revision_are_capped(monkeypatch):
    from scripts.swarm_ledger.api import task_list

    monkeypatch.setattr(task_list, "PAGES_KEPT", 1)
    repository = Repository(ledger(4, "One", "Two"))
    task_list.read(repository, "capped", {"limit": 1})
    task_list.read(repository, "capped", {"limit": 2})
    assert list(task_list.HELD["capped"].pages) == [(None, 2)]


def test_ledgers_held_are_capped_dropping_the_least_recent(monkeypatch):
    from scripts.swarm_ledger.api import task_list

    monkeypatch.setattr(task_list, "LEDGERS_KEPT", 2)
    monkeypatch.setattr(task_list, "HELD", {})
    repository = Repository(ledger(5, "Kept"))
    for slug in ("first", "second", "first", "third"):
        task_list.read(repository, slug, {"limit": 1})
    assert list(task_list.HELD) == ["first", "third"]


def test_rows_carry_the_revision_read_with_them_when_a_write_lands_between():
    from scripts.swarm_ledger.api import task_list

    repository = Repository(ledger(6, "Newer"))
    repository.task_version = lambda slug: (5, 0)
    reply = task_list.read(repository, "raced", {"limit": 1})
    assert reply["revision"] == f"{6:064x}"
    assert reply["data"][0]["title"] == "Newer"


def test_stored_task_version_reads_its_own_rows_and_moves_its_epoch_only_on_insert(tmp_path):
    from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository

    repository = SQLiteLedgerRepository(tmp_path / "ledger.sqlite3")
    repository.import_document("rows", {"title": "Rows", "tasks": [], "_meta": {"rev": 3}})
    revision, created = repository.task_version("rows")
    assert revision == 3 and created > 0
    time.sleep(0.002)
    repository.import_document("rows", {"title": "Rows", "_meta": {"rev": 4, "task_revision": 9}}, replace=True)
    revision, replaced = repository.task_version("rows")
    assert revision == 9 and replaced > created
    entry = repository.current("rows")
    state = {**entry.mutable(), "title": "Renamed"}
    state["_meta"]["rev"] = 5
    with repository.connect() as connection, connection:
        repository._write(connection, "rows", entry, state, [])
    assert repository.task_version("rows") == (9, replaced)
    assert repository.task_version("absent") == (0, 0)


def test_a_ledger_inserted_again_with_an_equal_task_revision_is_read_again(tmp_path):
    from scripts.swarm_ledger.api import task_list
    from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository

    repository = SQLiteLedgerRepository(tmp_path / "ledger.sqlite3")
    repository.import_document("again", {"title": "Again", **ledger(2, "Before")})
    assert task_list.read(repository, "again", {})["data"][0]["title"] == "Before"
    time.sleep(0.002)
    repository.import_document("again", {"title": "Again", **ledger(2, "After")}, replace=True)
    assert task_list.read(repository, "again", {})["data"][0]["title"] == "After"
