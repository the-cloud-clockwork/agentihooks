import copy

import pytest

from scripts.swarm_ledger.repository import entry as entry_module
from scripts.swarm_ledger.repository import sqlite
from scripts.swarm_ledger.repository.entry import Entry
from tests.swarm_ledger.test_repository_outbox import CHAT, CLAIM, store


def test_writer_copy_shares_only_stamp_records_and_public_copy_is_independent():
    stamp = {"at": 10, "rev": 2, "by": "reader"}
    state = {
        "_meta": {"rev": 2, "stamps": {"tasks/t1/done": stamp}, "events": [{"rev": 2}]},
        "tasks": [{"id": "t1", "comments": [{"text": "kept"}]}],
    }
    entry = Entry(2, None, state)
    writing = entry.for_write()
    assert writing == state
    assert list(writing) == list(state)
    assert list(writing["_meta"]) == list(state["_meta"])
    assert writing is not state
    assert writing["_meta"] is not state["_meta"]
    assert writing["_meta"]["stamps"] is not state["_meta"]["stamps"]
    assert writing["_meta"]["stamps"]["tasks/t1/done"] is stamp
    writing["_meta"]["stamps"]["tasks/t1/done"] = {"at": 20, "rev": 3, "by": "writer"}
    writing["_meta"]["events"][0]["rev"] = 3
    writing["tasks"][0]["comments"][0]["text"] = "changed"
    assert state["_meta"]["stamps"]["tasks/t1/done"] == {"at": 10, "rev": 2, "by": "reader"}
    assert state["_meta"]["events"] == [{"rev": 2}]
    assert state["tasks"][0]["comments"] == [{"text": "kept"}]
    public = entry.mutable()
    public["_meta"]["stamps"]["tasks/t1/done"]["at"] = 30
    assert stamp["at"] == 10


@pytest.mark.parametrize("outcome", ["success", "refused", "rollback"])
def test_writes_leave_the_previous_cached_state_unchanged(tmp_path, monkeypatch, outcome):
    repo = store(tmp_path)
    repo.apply_ops("ledger", ops=[CLAIM])
    with repo.connect() as connection:
        previous = repo._entry(connection, "ledger")
    before = copy.deepcopy(previous.state)
    assert before["_meta"]["stamps"]

    if outcome == "rollback":

        def broken(slug, state):
            raise RuntimeError("summary failed")

        monkeypatch.setattr(sqlite, "summarize", broken)
        with pytest.raises(RuntimeError, match="summary failed"):
            repo.apply_ops("ledger", ops=[CHAT])
        monkeypatch.undo()
        assert repo.get_document("ledger") == before
        assert sqlite.SQLiteLedgerRepository(repo.path).get_document("ledger") == before
    elif outcome == "refused":
        _, rejected = repo.apply_ops("ledger", changes=[{"path": "tasks/missing/done", "value": True}])
        assert rejected == ["tasks/missing/done"]
    else:
        written, rejected = repo.apply_ops("ledger", ops=[CHAT])
        assert rejected == []
        assert written["_meta"]["rev"] > before["_meta"]["rev"]
    assert previous.state == before


def test_atomic_refusal_restores_stamps_after_an_earlier_operation_wrote_one(tmp_path):
    repo = store(tmp_path)
    repo.apply_ops("ledger", ops=[CLAIM])
    with repo.connect() as connection:
        previous = repo._entry(connection, "ledger")
    before = copy.deepcopy(previous.state)
    ops = [
        {"op": "plan_add", "id": "plan", "by": "planner", "plan": "new", "title": "New plan"},
        {
            "op": "phase_update",
            "id": "changed",
            "by": "planner",
            "item": "phases/p1",
            "fields": {"title": "Changed"},
        },
        {
            "op": "phase_update",
            "id": "refused",
            "by": "planner",
            "item": "phases/missing",
            "fields": {"title": "Missing"},
        },
    ]
    written, rejected = repo.apply_ops("ledger", ops=ops)
    assert rejected == ["plan", "changed", "refused"]
    assert written["phases"] == before["phases"]
    assert written["plans"] == before["plans"]
    assert written["_meta"]["stamps"] == before["_meta"]["stamps"]
    assert previous.state == before


def containers(value: object, found: dict) -> dict:
    if type(value) in (dict, list):
        found[id(value)] = value
        for item in value.values() if type(value) is dict else value:
            containers(item, found)
    return found


def cached(repo):
    with repo.connect() as connection:
        return repo._entry(connection, "ledger")


def test_a_write_holds_an_independent_writer_copy_for_the_next_write(tmp_path):
    repo = store(tmp_path)
    repo.apply_ops("ledger", ops=[CLAIM])
    repo.apply_ops("ledger", ops=[CHAT])
    entry = cached(repo)
    held = entry.writer
    assert held == entry.state
    assert list(held) == list(entry.state)
    assert list(held["_meta"]) == list(entry.state["_meta"])
    stamps = {id(record) for record in entry.state["_meta"]["stamps"].values()}
    assert set(containers(held, {})) & set(containers(entry.state, {})) <= stamps
    before = copy.deepcopy(entry.state)
    for task in held["tasks"]:
        task["title"] = "changed by the writer"
    held["chat"].append({"id": "m9", "text": "writer only"})
    held["_meta"]["events"].clear()
    assert entry.state == before
    assert entry.for_write() is held
    assert entry.writer is None
    assert entry.for_write() is not held


def test_writes_through_held_copies_store_what_a_fresh_reader_loads(tmp_path):
    repo = store(tmp_path)
    for op in (CLAIM, CHAT, {**CHAT, "id": "m2", "text": "again"}):
        repo.apply_ops("ledger", ops=[op])
        entry = cached(repo)
        assert entry.writer == entry.state
        assert sqlite.SQLiteLedgerRepository(repo.path).get_document("ledger") == repo.get_document("ledger")
    assert [message["text"] for message in repo.get_document("ledger")["chat"]][-2:] == ["hello", "again"]


def test_a_failed_write_drops_the_held_copy_and_the_next_write_copies_again(tmp_path, monkeypatch):
    repo = store(tmp_path)
    repo.apply_ops("ledger", ops=[CLAIM])
    entry = cached(repo)
    assert entry.writer is not None

    def broken(slug, state):
        raise RuntimeError("summary failed")

    monkeypatch.setattr(sqlite, "summarize", broken)
    with pytest.raises(RuntimeError, match="summary failed"):
        repo.apply_ops("ledger", ops=[CHAT])
    monkeypatch.undo()
    assert cached(repo) is entry
    assert entry.writer is None
    _, rejected = repo.apply_ops("ledger", ops=[CHAT])
    assert rejected == []
    assert sqlite.SQLiteLedgerRepository(repo.path).get_document("ledger") == repo.get_document("ledger")


def test_a_mutation_that_raises_drops_the_held_copy(tmp_path, monkeypatch):
    import ledger_priorities

    repo = store(tmp_path)
    repo.apply_ops("ledger", ops=[CLAIM])
    entry = cached(repo)
    before = copy.deepcopy(entry.state)

    def broken(doc, ctx):
        doc["tasks"].clear()
        raise RuntimeError("derive failed")

    monkeypatch.setattr(ledger_priorities, "derive", broken)
    with pytest.raises(RuntimeError, match="derive failed"):
        repo.apply_ops("ledger", ops=[CHAT])
    monkeypatch.undo()
    assert entry.writer is None
    assert entry.state == before
    repo.apply_ops("ledger", ops=[CHAT])
    assert repo.get_document("ledger")["tasks"] == before["tasks"]


def test_a_write_through_the_held_copy_copies_only_the_leaves_it_changed(tmp_path, monkeypatch):
    repo = store(tmp_path)
    repo.apply_ops("ledger", ops=[CLAIM])
    tasks = [copy.deepcopy(task) for task in cached(repo).state["tasks"]]
    assert tasks
    copied = []
    real = entry_module.thawed

    def counting(value):
        copied.append(value)
        return real(value)

    monkeypatch.setattr(entry_module, "thawed", counting)
    published, _ = repo.apply_ops("ledger", ops=[CHAT], copy_state=False)
    assert published["tasks"] == tasks
    assert not [value for value in copied if isinstance(value, dict) and value in tasks]
    assert [value for value in copied if isinstance(value, dict) and value.get("text") == "hello"]
    published["chat"].clear()
    assert cached(repo).writer["chat"]


def test_a_reloaded_entry_holds_no_writer_copy_and_copies_on_write(tmp_path):
    repo = store(tmp_path)
    repo.apply_ops("ledger", ops=[CLAIM])
    reloaded = cached(sqlite.SQLiteLedgerRepository(repo.path))
    assert reloaded.writer is None
    writing = reloaded.for_write()
    assert writing == reloaded.state
    assert writing is not reloaded.state
    assert writing["tasks"] is not reloaded.state["tasks"]
