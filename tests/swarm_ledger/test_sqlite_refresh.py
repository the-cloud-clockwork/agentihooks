import json
import sqlite3

import pytest

from scripts.swarm_ledger.repository import sqlite
from tests.swarm_ledger.test_sqlite import CONTENT, chat, store

CLAIM = {"op": "task_update", "id": "u1", "by": "eng", "item": "tasks/t2", "fields": {"state": "claimed"}}
WRITES = {
    "task_row": [[CLAIM]],
    "new_item": [[chat(1)]],
    "edited_and_deleted_items": [[chat(1), chat(2)], [{"op": "edit", "id": "m1", "thread": "chat", "text": "e"}]],
    "member_join": [[{"op": "join", "id": "j1", "by": "eng"}]],
    "several_generations": [[chat(1)], [CLAIM], [{"op": "delete", "id": "m1", "thread": "chat"}]],
}


def whole(tmp_path):
    return store(tmp_path).current("ledger")


@pytest.mark.parametrize("writes", WRITES.values(), ids=WRITES.keys())
def test_a_foreign_write_refreshes_only_changed_rows_and_serves_the_whole_rebuild(tmp_path, monkeypatch, writes):
    reader, writer = store(tmp_path), store(tmp_path)
    reader.create("ledger", CONTENT)
    reader.apply_ops("ledger", ops=[chat(0)])
    reader.current("ledger")
    for ops in writes:
        writer.apply_ops("ledger", ops=ops)
    rebuilds = []
    original = sqlite.read_rows
    monkeypatch.setattr(sqlite, "read_rows", lambda *a: rebuilds.append(a) or original(*a))
    served = reader.current("ledger")
    assert rebuilds == []
    expected = whole(tmp_path)
    assert served.generation == expected.generation
    assert served.text == expected.text
    assert served.state == expected.state
    state, _ = reader.apply_ops("ledger", ops=[chat(9)])
    assert state == whole(tmp_path).state


def test_a_foreign_task_write_refreshes_the_task_and_its_events(tmp_path):
    reader, writer = store(tmp_path), store(tmp_path)
    reader.create("ledger", CONTENT)
    reader.current("ledger")
    written, _ = writer.apply_ops("ledger", ops=[CLAIM])
    served = reader.current("ledger").state
    assert served["tasks"][1]["state"] == "claimed"
    assert served["_meta"]["events"] == written["_meta"]["events"]


def test_a_generation_without_its_change_record_falls_back_to_the_whole_rebuild(tmp_path, monkeypatch):
    reader, writer = store(tmp_path), store(tmp_path)
    reader.create("ledger", CONTENT)
    reader.current("ledger")
    writer.apply_ops("ledger", ops=[chat(1)])
    writer.apply_ops("ledger", ops=[CLAIM])
    with sqlite3.connect(writer.path) as connection:
        connection.execute("DELETE FROM row_changes WHERE generation=(SELECT MIN(generation) FROM row_changes)")
    rebuilds = []
    original = sqlite.read_rows
    monkeypatch.setattr(sqlite, "read_rows", lambda *a: rebuilds.append(a) or original(*a))
    served = reader.current("ledger")
    assert len(rebuilds) == 1
    assert served.text == whole(tmp_path).text


def test_a_replaced_ledger_falls_back_to_the_whole_rebuild(tmp_path, monkeypatch):
    reader, writer = store(tmp_path), store(tmp_path)
    reader.create("ledger", CONTENT)
    reader.current("ledger")
    writer.import_document("ledger", {**json.loads(reader.current("ledger").text), "title": "New"}, replace=True)
    rebuilds = []
    original = sqlite.read_rows
    monkeypatch.setattr(sqlite, "read_rows", lambda *a: rebuilds.append(a) or original(*a))
    assert reader.current("ledger").state["title"] == "New"
    assert len(rebuilds) == 1


def test_change_records_keep_only_recent_generations(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite, "CHANGES_KEPT", 2)
    repo = store(tmp_path)
    repo.create("ledger", CONTENT)
    for n in range(4):
        repo.apply_ops("ledger", ops=[chat(n)])
    generation = repo.current("ledger").generation
    with sqlite3.connect(repo.path) as connection:
        kept = [row[0] for row in connection.execute("SELECT generation FROM row_changes ORDER BY generation")]
    assert kept == [generation - 1, generation]
