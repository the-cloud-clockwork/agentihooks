import copy
import gc
import json
import sqlite3
import weakref

import pytest

from scripts.swarm_ledger.repository.rows import assemble, diff, encode, flatten, read_rows, write_rows
from scripts.swarm_ledger.repository.sqlite import ROWS_SCHEMA, SQLiteLedgerRepository
from tests.swarm_ledger.test_sqlite import document


@pytest.mark.parametrize(
    "value",
    [
        None,
        False,
        0,
        1.25,
        "é",
        [],
        {},
        {"a/b": {"": [False, None]}, "tasks": [{"id": "a"}, {"id": "a"}, {"text": "no id"}]},
        {"tasks": [{"id": "x", "comments": [{"id": "c", "answers": []}]}]},
    ],
)
def test_normalized_rows_preserve_complete_json_values(value):
    assert assemble(flatten(value)) == value


@pytest.mark.parametrize("malformed", [False, True])
def test_assembly_releases_rows_without_cyclic_collection(malformed, monkeypatch):
    class Rows(dict):
        pass

    class Score(float):
        pass

    decoder = json.JSONDecoder(parse_float=Score)
    monkeypatch.setattr(json, "loads", decoder.decode)
    rows = Rows(flatten({"score": 1.25, "tasks": [{"id": "task", "comments": []}]}))
    reference = weakref.ref(rows)
    if malformed:
        rows["[]"] = (*rows["[]"][:4], "value", "{")
    enabled = gc.isenabled()
    gc.disable()
    try:
        if malformed:
            with pytest.raises(json.JSONDecodeError):
                assemble(rows)
        else:
            rebuilt = assemble(rows)
            assert rebuilt == {"score": 1.25, "tasks": [{"id": "task", "comments": []}]}
            output_reference = weakref.ref(rebuilt["score"])
            del rebuilt
            assert output_reference() is None
        del rows
        assert reference() is None
    finally:
        if enabled:
            gc.enable()


def test_reorder_move_and_field_removal_preserve_other_items(tmp_path):
    repo = SQLiteLedgerRepository(tmp_path / "ledgers.sqlite3")
    before = document()
    repo.import_document("rows", before)
    after = copy.deepcopy(before)
    after["tasks"].reverse()
    after["tasks"][0].pop("unknown")
    after["extension"] = [{"id": "external", "comments": []}]
    after["tasks"][0]["comments"] = []
    repo.import_document("rows", after, replace=True)
    assert repo.export_document("rows") == after


def test_storage_rows_are_normalized_and_idempotent(tmp_path):
    repo = SQLiteLedgerRepository(tmp_path / "ledgers.sqlite3")
    state = document()
    repo.import_document("rows", state)
    with repo.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM resources WHERE slug=?", ("rows",)).fetchone()[0] > 0
        assert connection.execute("SELECT COUNT(*) FROM threads WHERE slug=?", ("rows",)).fetchone()[0] > 0
        assert connection.execute("SELECT COUNT(*) FROM fields WHERE slug=?", ("rows",)).fetchone()[0] > 0
        for table in ("fields", "resources", "threads"):
            for (path,) in connection.execute(f"SELECT path FROM {table} WHERE slug=?", ("rows",)):
                parts = json.loads(path)
                if any(part in ("comments", "answers") for part in parts if isinstance(part, str)):
                    assert table == "threads"
                elif parts and parts[0] in (
                    "tasks",
                    "questions",
                    "artifacts",
                    "artifact_trash",
                    "notifications",
                    "priorities",
                ):
                    assert table == "resources"
                else:
                    assert table == "fields"


def test_missing_and_null_delta_markers_are_distinct():
    from scripts.swarm_ledger.repository.rows import changes

    assert changes({}, {"gone": None}) == {"gone": None}
    assert changes({"gone": None}, {}) == {"gone": None}
    assert changes({"same": None}, {"same": None}) == {}
    assert changes({"same": 1}, {"same": 1}) == {}


def test_unicode_encoding_is_compact_and_nonfinite_values_are_refused():
    from scripts.swarm_ledger.repository.rows import encode

    assert encode({"name": "é", "items": [True, None]}) == '{"name":"é","items":[true,null]}'
    for value in (float("nan"), float("inf"), -float("inf")):
        with pytest.raises(ValueError):
            encode(value)


def test_normalized_row_identity_kind_and_order_are_stable():
    value = {"tasks": [{"id": "one"}, {"id": "two"}, {"id": "one"}, {"name": "anonymous"}]}
    rows = flatten(value)
    assert rows["[]"] == ("fields", None, "null", 0, "object", "null")
    roots = {path: (row[2], row[3], row[4], row[5]) for path, row in rows.items() if row[1] == '["tasks"]'}
    assert roots == {
        '["tasks",["id","one",0]]': ("0", 0, "object", "null"),
        '["tasks",["id","two",0]]': ("1", 1, "object", "null"),
        '["tasks",["id","one",1]]': ("2", 2, "object", "null"),
        '["tasks",["index",3,0]]': ("3", 3, "object", "null"),
    }
    assert assemble(rows) == value


def test_diff_descends_only_into_the_changed_leaf():
    old = {"a": 1, "b": {"x": 1, "y": 2}}
    new = {"a": 1, "b": {"x": 1, "y": 3}}
    before, after = diff(old, new)
    assert set(before) == set(after) == {encode(["b", "y"])}
    assert json.loads(before[encode(["b", "y"])][5]) == 2
    assert json.loads(after[encode(["b", "y"])][5]) == 3


def test_diff_keeps_the_real_parent_key_position_and_table_when_falling_back():
    before, after = diff({"a": {"x": 1}}, {"a": [1]})
    row = before[encode(["a"])]
    assert row[0] == "fields"
    assert row[1] == encode([])
    assert row[2] == encode("a")
    assert row[3] == 0


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ({1: "old"}, {True: "new"}),
        ({True: "old"}, {1: "new"}),
        ([{"id": True, "value": "old"}], [{"id": 1, "value": "new"}]),
        ([{"id": 1.0, "value": "old"}], [{"id": 1, "value": "new"}]),
        ([{"id": "x", "value": "old"}], [{"id": {"nested": 1}, "value": "new"}]),
        ([{"id": None, "value": "old"}], [{"id": "x", "value": "new"}]),
        (
            {"question": {"answers": [{"id": "a", "text": "old"}], "tail": 1}},
            {"question": {"answers": [{"id": "a", "text": "new"}, {"id": "b", "text": "added"}], "tail": 1}},
        ),
        (
            {"tasks": [{"id": "x", "comments": [{"id": "c", "text": "old"}]}]},
            {"tasks": [{"id": "x", "comments": [{"id": "c", "text": "new"}], "added": None}]},
        ),
        (
            [{"id": "same", "text": "one"}, {"id": "same", "text": "two"}],
            [{"id": "same", "text": "three"}, {"id": "same", "text": "four"}],
        ),
    ],
)
def test_applying_row_deltas_matches_the_complete_stored_tree(old, new):
    before, after = diff(old, new)
    stored = flatten(old)
    for path in before:
        stored.pop(path, None)
    stored.update(after)
    assert stored == flatten(new)


def test_diff_treats_a_root_type_change_as_position_zero():
    before, after = diff({"a": 1}, [1])
    assert before[encode([])][3] == 0
    assert after[encode([])][3] == 0


def test_changed_list_scalar_keeps_literal_stored_row_fields():
    old = {"values": [1, 2]}
    new = {"values": [1, 3]}
    before, after = diff(old, new)
    path = '["values",["index",1,0]]'
    assert before == {path: ("fields", '["values"]', "1", 1, "value", "2")}
    assert after == {path: ("fields", '["values"]', "1", 1, "value", "3")}
    stored = flatten(old)
    stored.update(after)
    assert assemble(stored) == new


def test_numeric_list_ids_keep_distinct_literal_occurrence_paths():
    stored = flatten([{"id": True}, {"id": 1}])
    assert set(stored) == {
        "[]",
        '[["id",true,0]]',
        '[["id",true,0],"id"]',
        '[["id",1,0]]',
        '[["id",1,0],"id"]',
    }
    assert stored['[["id",true,0]]'][3] == 0
    assert stored['[["id",1,0]]'][3] == 1


class Recorder:
    def __init__(self):
        self.calls = []

    def execute(self, sql, parameters=()):
        self.calls.append(("execute", sql, [parameters]))

    def executemany(self, sql, rows):
        self.calls.append(("executemany", sql, list(rows)))


def receipts(first):
    return {"_meta": {"api_operations": {f"op{n}": {"digest": f"d{n}"} for n in range(first, first + 1000)}}}


def test_a_write_sends_its_changed_rows_as_one_batch_per_table_and_kind():
    before, after = diff(receipts(0), receipts(1))
    recorder = Recorder()
    write_rows(recorder, "ledger", before, after)
    assert [(kind, sql.split()[0], len(rows)) for kind, sql, rows in recorder.calls] == [
        ("executemany", "DELETE", 2),
        ("executemany", "INSERT", 1001),
    ]


def test_batched_row_writes_store_the_new_tree():
    connection = sqlite3.connect(":memory:")
    connection.executescript(ROWS_SCHEMA)
    old, new = receipts(0), {**receipts(1), "chat": [{"id": "m1", "text": "moved"}]}
    write_rows(connection, "ledger", {}, flatten(old))
    write_rows(connection, "ledger", *diff(old, new))
    assert read_rows(connection, "ledger") == flatten(new)
