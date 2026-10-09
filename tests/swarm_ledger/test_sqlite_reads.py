import sqlite3

import pytest

from scripts.swarm_ledger.repository import sqlite
from scripts.swarm_ledger.repository.rows import encode
from scripts.swarm_ledger.repository.sqlite import (
    DATABASE,
    Missing,
    SQLiteLedgerRepository,
    key_parts,
    read_ids,
    read_ledger,
    read_only,
    read_partial,
    read_registry,
    scope,
    summarize,
    without_events,
)
from tests.swarm_ledger.test_sqlite import document


def stored(tmp_path, state=None, registries=None):
    folder = tmp_path / "ledgers"
    repo = SQLiteLedgerRepository(folder / DATABASE)
    repo.import_document("ledger", state or document())
    with repo.connect() as connection, connection:
        for name, entries in (registries or {"bin": {"a": 1, "b": [2, 3]}}).items():
            repo.save_registry(connection, name, entries)
    return folder


def empty_database(tmp_path):
    folder = tmp_path / "bare"
    folder.mkdir()
    sqlite3.connect(folder / DATABASE).close()
    return folder


def test_the_database_name_and_queries_are_fixed():
    assert DATABASE == "ledgers.sqlite3"
    assert sqlite.BEGIN == "BEGIN"
    assert sqlite.REGISTRY == "SELECT path,value FROM registry WHERE slug=?"
    assert sqlite.PATH_END == "\x7f"


def test_a_missing_ledger_is_both_a_lookup_and_a_value_error_naming_its_slug():
    error = Missing("ledger")
    assert isinstance(error, KeyError) and isinstance(error, ValueError)
    assert error.args == ("ledger",)


def test_summarize_counts_done_filters_out_of_scope_and_wires_helpers():
    state = {
        "title": "Store",
        "overview": "hello world\n\nSummary\nclosing words",
        "size": "small",
        "closed_at": 99,
        "tasks": [{"done": True}, {"done": False}, {"out_of_scope": True, "done": True}, {"done": "yes"}],
        "_meta": {"updated_at": 10, "created_at": 5},
    }
    assert summarize("ledger", state) == {
        "slug": "ledger",
        "title": "Store",
        "overview": "hello world",
        "closed_at": 99,
        "size": "small",
        "open": 2,
        "done": 1,
        "updated_at": 10,
        "created_at": 5,
        "finished": False,
    }


def test_summarize_falls_back_to_phases_title_and_defaults_when_tasks_are_absent():
    state = {"tasks": [], "phases": [{"done": True}, {"done": True}], "overview": None, "title": ""}
    assert summarize("ledger", state) == {
        "slug": "ledger",
        "title": "ledger",
        "overview": "",
        "closed_at": None,
        "size": "swarm",
        "open": 0,
        "done": 2,
        "updated_at": None,
        "created_at": None,
        "finished": True,
    }


def test_summarize_of_an_empty_ledger_counts_nothing():
    assert summarize("ledger", {})["open"] == 0
    assert summarize("ledger", {})["done"] == 0


def test_scope_lists_every_ancestor_path_and_the_descendant_prefix():
    exact, prefix = scope(["tasks", "t1"])
    assert exact == [encode([]), encode(["tasks"]), encode(["tasks", "t1"])]
    assert prefix == encode(["tasks", "t1"])[:-1] + ","
    assert scope([]) == ([encode([])], encode([])[:-1] + ",")


def test_key_parts_turn_names_dots_and_item_ids_into_path_parts():
    assert key_parts("tasks") == ["tasks"]
    assert key_parts("_meta.members") == ["_meta", "members"]
    assert key_parts("tasks/t1") == ["tasks", ["id", "t1", 0]]
    assert key_parts("_meta.stamps/x") == ["_meta", "stamps", ["id", "x", 0]]


def test_without_events_drops_only_the_event_log_and_leaves_the_input_alone():
    state = {"title": "t", "_meta": {"rev": 2, "events": [1], "members": {}}}
    assert without_events(state) == {"title": "t", "_meta": {"rev": 2, "members": {}}}
    assert state["_meta"]["events"] == [1]


def test_read_only_yields_none_without_a_database_and_a_connection_that_refuses_writes(tmp_path):
    with read_only(tmp_path) as connection:
        assert connection is None
    folder = stored(tmp_path)
    with read_only(folder) as connection:
        assert connection.execute("SELECT slug FROM ledgers").fetchall() == [("ledger",)]
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM ledgers")
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def test_read_only_accepts_a_folder_given_as_text(tmp_path):
    folder = stored(tmp_path)
    with read_only(str(folder)) as connection:
        assert connection.execute("SELECT count(*) FROM ledgers").fetchone() == (1,)


def test_read_partial_returns_only_the_named_item_and_the_root(tmp_path):
    state = document()
    state["tasks"].append({"id": "first2", "comments": []})
    folder = stored(tmp_path, state)
    with read_only(folder) as connection:
        part = read_partial(connection, "ledger", (key_parts("tasks/first"),))
    assert list(part) == ["tasks"]
    assert [task["id"] for task in part["tasks"]] == ["first"]
    assert part["tasks"][0]["unknown"] == {"a/b": [None, False, 0]}


def test_read_partial_fills_the_event_log_from_its_table_only_when_asked(tmp_path):
    folder = stored(tmp_path)
    with read_only(folder) as connection:
        events = read_partial(connection, "ledger", (["_meta", "events"],))
        members = read_partial(connection, "ledger", (["_meta", "members"],))
    assert events == {"_meta": {"events": [{"rev": 1, "id": "op", "unknown": {"x": True}}]}}
    assert members == {"_meta": {"members": {"eng": {"handled_rev": 0}}}}


def test_read_partial_joins_several_keys(tmp_path):
    folder = stored(tmp_path)
    with read_only(folder) as connection:
        part = read_partial(connection, "ledger", (["title"], key_parts("questions/q1")))
    assert part == {"title": "Café", "questions": [{"id": "q1", "answers": [{"id": "a1", "text": "yes"}]}]}


def test_read_partial_refuses_a_ledger_that_is_not_stored(tmp_path):
    folder = stored(tmp_path)
    with read_only(folder) as connection, pytest.raises(Missing) as caught:
        read_partial(connection, "other", (["title"],))
    assert caught.value.args == ("other",)


def test_read_ledger_reads_named_parts_or_answers_none(tmp_path):
    folder = stored(tmp_path)
    assert read_ledger(folder, "ledger", "title") == {"title": "Café"}
    assert read_ledger(folder, "ledger", "tasks/second", "_meta.members") == {
        "tasks": [{"id": "second", "proof": {"output": "verified"}, "comments": []}],
        "_meta": {"members": {"eng": {"handled_rev": 0}}},
    }
    assert read_ledger(folder, "missing", "title") is None
    assert read_ledger(tmp_path / "nowhere", "ledger", "title") is None
    assert read_ledger(empty_database(tmp_path), "ledger", "title") is None


def test_read_registry_decodes_values_or_answers_empty(tmp_path):
    folder = stored(tmp_path)
    assert read_registry(folder, "bin") == {"a": 1, "b": [2, 3]}
    assert read_registry(folder, "other") == {}
    assert read_registry(tmp_path / "nowhere", "bin") == {}
    assert read_registry(empty_database(tmp_path), "bin") == {}


def test_read_ids_lists_a_collection_in_stored_order(tmp_path):
    state = document()
    state["sources"] = ["a", "b"]
    folder = stored(tmp_path, state)
    assert read_ids(folder, "ledger", "tasks") == ("second", "first")
    assert read_ids(folder, "ledger", "questions") == ("q1",)
    assert read_ids(folder, "ledger", "sources") == ()
    assert read_ids(folder, "missing", "tasks") == ()
    assert read_ids(tmp_path / "nowhere", "ledger", "tasks") == ()
    assert read_ids(empty_database(tmp_path), "ledger", "tasks") == ()
