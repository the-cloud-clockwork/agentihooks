import sqlite3
import tracemalloc

import pytest

pytestmark = pytest.mark.unit

SLUG = "ledger"


def stored(value: object, reverse: bool = False) -> sqlite3.Connection:
    from scripts.swarm_ledger.repository.rows import flatten, write_rows
    from scripts.swarm_ledger.repository.sqlite import ROWS_SCHEMA

    connection = sqlite3.connect(":memory:")
    connection.executescript(ROWS_SCHEMA)
    rows = flatten(value)
    write_rows(connection, SLUG, {}, dict(reversed(rows.items())) if reverse else rows)
    return connection


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    "value",
    [
        {},
        [],
        {"z": 1, "a": [3, 1, 2], "m": {"y": None, "b": "é"}},
        {"tasks": [{"id": "a", "comments": [{"id": "c", "answers": []}]}, {"id": "a"}], "chat": [{"id": "m"}]},
        [[{"k": [0, {"x": False}]}], 1.25, "s"],
    ],
)
def test_load_gives_the_assembled_ledger_in_stored_order(value, reverse):
    from scripts.swarm_ledger.repository.rows import assemble, load, read_rows

    connection = stored(value, reverse)
    loaded = load(connection, SLUG)
    assert loaded == assemble(read_rows(connection, SLUG)) == value
    assert repr(loaded) == repr(value)


def test_load_skips_rows_whose_parent_is_not_stored():
    from scripts.swarm_ledger.repository.rows import load

    connection = stored({"a": [1]})
    connection.execute(
        "INSERT INTO fields (slug, path, parent, key, position, kind, value) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (SLUG, '["gone","x"]', '["gone"]', '"x"', 0, "value", "7"),
    )
    assert load(connection, SLUG) == {"a": [1]}


def test_load_reads_only_the_named_ledger():
    from scripts.swarm_ledger.repository.rows import flatten, load, write_rows

    connection = stored({"a": 1})
    write_rows(connection, "other", {}, flatten({"b": [2]}))
    assert load(connection, SLUG) == {"a": 1}
    with pytest.raises(KeyError):
        load(connection, "missing")


def test_a_cold_ledger_read_never_holds_every_stored_row_at_once(tmp_path, monkeypatch):
    from scripts.swarm_ledger.repository import sqlite
    from tests.swarm_ledger.test_task_list_memory import document

    doc = document()
    sqlite.SQLiteLedgerRepository(tmp_path / "ledger.sqlite3").import_document(SLUG, doc)
    del doc

    def refused(connection, slug):
        raise AssertionError("whole row set read")

    monkeypatch.setattr(sqlite, "read_rows", refused)
    repository = sqlite.SQLiteLedgerRepository(tmp_path / "ledger.sqlite3")
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        state = repository.current(SLUG).state
        held, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(state["tasks"]) == 2000
    assert peak < 3 * held, (peak, held)
