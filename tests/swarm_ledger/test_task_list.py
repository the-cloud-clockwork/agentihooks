import json

import pytest

pytestmark = pytest.mark.unit


class Repository:
    def __init__(self, state: dict) -> None:
        self.state, self.reads = state, 0

    def task_revision(self, slug: str) -> int:
        return self.state["_meta"]["task_revision"]

    def read(self, slug: str, *keys: str) -> dict:
        assert keys == ("tasks",)
        self.reads += 1
        return {key: value for key, value in self.state.items() if key == "tasks"}


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


def test_pages_kept_per_revision_are_capped(monkeypatch):
    from scripts.swarm_ledger.api import task_list

    monkeypatch.setattr(task_list, "PAGES_KEPT", 1)
    repository = Repository(ledger(4, "One", "Two"))
    task_list.read(repository, "capped", {"limit": 1})
    task_list.read(repository, "capped", {"limit": 2})
    assert list(task_list.HELD["capped"].pages) == [(None, 2)]


def test_stored_task_revision_reads_its_own_row_and_falls_back_to_the_ledger_rev(tmp_path):
    from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository

    repository = SQLiteLedgerRepository(tmp_path / "ledger.sqlite3")
    repository.import_document("rows", {"title": "Rows", "tasks": [], "_meta": {"rev": 3}})
    assert repository.task_revision("rows") == 3
    repository.import_document("rows", {"title": "Rows", "_meta": {"rev": 4, "task_revision": 9}}, replace=True)
    assert repository.task_revision("rows") == 9
    assert repository.task_revision("absent") == 0
