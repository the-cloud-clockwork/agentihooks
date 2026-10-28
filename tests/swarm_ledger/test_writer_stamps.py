import copy

import pytest

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


def test_atomic_refusal_restores_stamps_after_an_earlier_operation_replaced_one(tmp_path):
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
