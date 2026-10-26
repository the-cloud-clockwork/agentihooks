import sqlite3
import tracemalloc

import pytest

from scripts.swarm_ledger.repository import sqlite
from scripts.swarm_ledger.repository.rows import assemble, flatten, load, read_rows, write_rows

pytestmark = pytest.mark.unit

SLUG = "ledger"


def stored(value: object) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(sqlite.ROWS_SCHEMA)
    write_rows(connection, SLUG, {}, flatten(value))
    return connection


@pytest.mark.parametrize(
    "value",
    [
        {},
        [],
        {"z": 1, "a": [3, 1, 2], "m": {"y": None, "b": "é"}},
        {"tasks": [{"id": "b"}, {"id": "a", "comments": [{"id": "d"}, {"id": "c", "answers": []}]}, {"id": "b"}]},
        [[{"k": [0, {"x": False}]}], 1.25, "s"],
    ],
)
def test_load_gives_the_assembled_ledger_in_stored_order(value):
    connection = stored(value)
    loaded = load(connection, SLUG)
    assert loaded == assemble(read_rows(connection, SLUG)) == value
    assert repr(loaded) == repr(value)


def test_load_skips_rows_whose_parent_is_not_stored():
    connection = stored({"a": [1]})
    connection.execute(
        "INSERT INTO fields (slug, path, parent, key, position, kind, value) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (SLUG, '["gone","x"]', '["gone"]', '"x"', 0, "value", "7"),
    )
    assert load(connection, SLUG) == {"a": [1]}


def test_load_reads_only_the_named_ledger():
    connection = stored({"a": 1})
    write_rows(connection, "other", {}, flatten({"b": [2]}))
    assert load(connection, SLUG) == {"a": 1}
    with pytest.raises(KeyError):
        load(connection, "missing")


def test_load_reads_through_the_connection_stream_when_it_has_one():
    inner = stored({"a": [1, {"b": 2}]})
    streamed = []

    class Streaming:
        def stream(self, sql, parameters):
            streamed.append(sql)
            return inner.execute(sql, parameters)

        def execute(self, sql, parameters):
            raise AssertionError("buffered read")

    assert load(Streaming(), SLUG) == {"a": [1, {"b": 2}]}
    assert len(streamed) == 6


def test_a_cold_ledger_read_never_holds_every_stored_row_at_once(tmp_path, monkeypatch):
    from tests.swarm_ledger.test_task_list_memory import document

    path = tmp_path / "ledger.sqlite3"
    doc = document()
    sqlite.SQLiteLedgerRepository(path).import_document(SLUG, doc)
    del doc

    def refused(connection, slug):
        raise AssertionError("whole row set read")

    monkeypatch.setattr(sqlite, "read_rows", refused)
    repository = sqlite.SQLiteLedgerRepository(path)
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        state = repository.current(SLUG).state
        held, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 3 * held, (peak, held)
    with sqlite3.connect(path) as connection:
        assert state == assemble(read_rows(connection, SLUG))
    assert len(state["tasks"]) == 2000
