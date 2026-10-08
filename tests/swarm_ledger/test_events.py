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
    repo.import_document("events", after, replace=True)
    assert repo.export_document("events") == after
    assert repo.events_since("events", 2) == [{"rev": 3, "id": "last"}]


def test_unversioned_event_records_are_preserved(tmp_path):
    state = document()
    state["_meta"]["events"].append({"kind": "legacy", "extension": {"x": False}})
    repo = SQLiteLedgerRepository(tmp_path / "shadow.sqlite3")
    repo.import_document("events", state)
    assert repo.export_document("events") == state
    with pytest.raises(KeyError) as error:
        repo.events_since("events", 0)
    assert error.value.args == ("rev",)


def test_three_identical_events_remain_distinct_records(tmp_path):
    state = document()
    state["_meta"]["events"] = [{"rev": 2, "id": "duplicate"}] * 3
    repo = SQLiteLedgerRepository(tmp_path / "shadow.sqlite3")
    repo.import_document("events", state)
    assert repo.export_document("events") == state
    assert repo.events_since("events", 1) == state["_meta"]["events"]


def test_event_rows_keep_canonical_identity_after_restart(tmp_path):
    state = document()
    state["_meta"]["events"] = [{"rev": 2, "id": "duplicate"}] * 3
    repo = SQLiteLedgerRepository(tmp_path / "shadow.sqlite3")
    repo.import_document("events", state)
    with repo.connect() as connection:
        assert connection.execute("SELECT key,position FROM events ORDER BY position").fetchall() == [
            ("8210d9e742857ecd0ea706c81cffbbbd1c175f705b43292a8403ccc5118c094f:0", 0),
            ("8210d9e742857ecd0ea706c81cffbbbd1c175f705b43292a8403ccc5118c094f:1", 1),
            ("8210d9e742857ecd0ea706c81cffbbbd1c175f705b43292a8403ccc5118c094f:2", 2),
        ]
    restarted = SQLiteLedgerRepository(repo.path)
    trace = []
    restarted.trace = trace.append
    assert restarted.export_document("events")["_meta"]["events"] == state["_meta"]["events"]
    assert not [sql for sql in trace if sql.startswith(("INSERT", "UPDATE", "DELETE"))]
