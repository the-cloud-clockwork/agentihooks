import copy

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
    repo = SQLiteLedgerRepository(tmp_path / "shadow.sqlite3")
    before = document()
    repo.import_document("rows", before)
    after = copy.deepcopy(before)
    after["tasks"].reverse()
    after["tasks"][0].pop("unknown")
    after["extension"] = [{"id": "external", "comments": []}]
    after["tasks"][0]["comments"] = []
    repo.import_document("rows", after)
    assert repo.get_document("rows") == after
