import copy
import json

import pytest

from scripts.swarm_ledger.repository.rows import assemble, flatten
from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository
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
