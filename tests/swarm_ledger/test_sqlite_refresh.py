import json
import sqlite3

import pytest

from scripts.swarm_ledger.repository import refresh, sqlite
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
    cached = reader.current("ledger")
    for ops in writes:
        writer.apply_ops("ledger", ops=ops)
    rebuilds = []
    original = sqlite.load
    monkeypatch.setattr(sqlite, "load", lambda *a: rebuilds.append(a) or original(*a))
    served = reader.current("ledger")
    assert rebuilds == []
    expected = whole(tmp_path)
    assert served.generation == expected.generation
    assert served.text == expected.text
    assert served.state == expected.state
    assert served.fragments.parts["phases"] is cached.fragments.parts["phases"]
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
    original = sqlite.load
    monkeypatch.setattr(sqlite, "load", lambda *a: rebuilds.append(a) or original(*a))
    served = reader.current("ledger")
    assert len(rebuilds) == 1
    assert served.text == whole(tmp_path).text


def test_a_replaced_ledger_falls_back_to_the_whole_rebuild(tmp_path, monkeypatch):
    reader, writer = store(tmp_path), store(tmp_path)
    reader.create("ledger", CONTENT)
    reader.current("ledger")
    writer.import_document("ledger", {**json.loads(reader.current("ledger").text), "title": "New"}, replace=True)
    rebuilds = []
    original = sqlite.load
    monkeypatch.setattr(sqlite, "load", lambda *a: rebuilds.append(a) or original(*a))
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


def test_a_write_past_the_recorded_path_limit_falls_back_to_the_whole_rebuild(tmp_path, monkeypatch):
    monkeypatch.setattr(refresh, "PATHS_KEPT", 1)
    reader, writer = store(tmp_path), store(tmp_path)
    reader.create("ledger", CONTENT)
    reader.current("ledger")
    writer.apply_ops("ledger", ops=[chat(1)])
    rebuilds = []
    original = sqlite.load
    monkeypatch.setattr(sqlite, "load", lambda *a: rebuilds.append(a) or original(*a))
    served = reader.current("ledger")
    assert len(rebuilds) == 1
    assert served.text == whole(tmp_path).text


@pytest.mark.parametrize(("kept", "whole_rebuilds"), [(2, 0), (1, 1)])
def test_a_reader_refreshes_only_while_its_gap_fits_the_kept_generations(tmp_path, monkeypatch, kept, whole_rebuilds):
    monkeypatch.setattr(sqlite, "CHANGES_KEPT", kept)
    reader, writer = store(tmp_path), store(tmp_path)
    reader.create("ledger", CONTENT)
    reader.current("ledger")
    writer.apply_ops("ledger", ops=[chat(1)])
    writer.apply_ops("ledger", ops=[CLAIM])
    rebuilds = []
    original = sqlite.load
    monkeypatch.setattr(sqlite, "load", lambda *a: rebuilds.append(a) or original(*a))
    served = reader.current("ledger")
    assert len(rebuilds) == whole_rebuilds
    assert served.text == whole(tmp_path).text


def test_changed_paths_unite_every_recorded_generation_in_the_gap(tmp_path, monkeypatch):
    monkeypatch.setattr(refresh, "PATHS_KEPT", 2)
    repo = store(tmp_path)
    repo.create("ledger", CONTENT)
    with repo.connect() as connection, connection:
        refresh.record(connection, "ledger", 11, ['["b"]', '["a"]'], 8)
        refresh.record(connection, "ledger", 12, ['["c"]'], 8)
        assert refresh.changed_paths(connection, "ledger", 10, 12, 8) == {'["a"]', '["b"]', '["c"]'}
        assert refresh.changed_paths(connection, "ledger", 11, 12, 8) == {'["c"]'}
        assert refresh.changed_paths(connection, "ledger", 9, 12, 8) is None
        assert refresh.changed_paths(connection, "ledger", 10, 12, 1) is None
        stored = connection.execute("SELECT paths FROM row_changes WHERE generation=11").fetchone()[0]
    assert stored == '["[\\"a\\"]","[\\"b\\"]"]'


def test_touched_names_each_changed_path_and_every_container_above_it():
    assert refresh.touched({'["tasks",["id","t1",0],"state"]'}) == {
        "[]",
        '["tasks"]',
        '["tasks",["id","t1",0]]',
        '["tasks",["id","t1",0],"state"]',
    }
