import copy

import pytest

from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository
from tests.swarm_ledger.test_sqlite import document


def test_ordered_event_rows_keep_duplicates_and_revision_filter(tmp_path):
    repo = SQLiteLedgerRepository(tmp_path / "shadow.sqlite3")
    state = document()
    state["_meta"]["events"] = [{"rev": 2, "id": "repeat"}, {"rev": 1, "id": "first"}, {"rev": 2, "id": "repeat"}]
    repo.import_document("events", state)
    assert repo.events_since("events", 0) == state["_meta"]["events"]
    assert repo.events_since("events", 1) == [{"rev": 2, "id": "repeat"}, {"rev": 2, "id": "repeat"}]
    after = copy.deepcopy(state)
    after["_meta"]["events"] = [{"rev": 1, "id": "first"}, {"rev": 2, "id": "repeat"}, {"rev": 3, "id": "last"}]
    repo.import_document("events", after)
    assert repo.get_document("events") == after
    assert repo.events_since("events", 2) == [{"rev": 3, "id": "last"}]


def test_unversioned_event_records_are_preserved(tmp_path):
    state = document()
    state["_meta"]["events"].append({"kind": "legacy", "extension": {"x": False}})
    repo = SQLiteLedgerRepository(tmp_path / "shadow.sqlite3")
    repo.import_document("events", state)
    assert repo.get_document("events") == state
    with pytest.raises(KeyError) as error:
        repo.events_since("events", 0)
    assert error.value.args == ("rev",)


def test_three_identical_events_remain_distinct_records(tmp_path):
    state = document()
    state["_meta"]["events"] = [{"rev": 2, "id": "duplicate"}] * 3
    repo = SQLiteLedgerRepository(tmp_path / "shadow.sqlite3")
    repo.import_document("events", state)
    assert repo.get_document("events") == state
    assert repo.events_since("events", 1) == state["_meta"]["events"]
