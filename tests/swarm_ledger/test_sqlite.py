import copy
import json

import pytest

from scripts.swarm_ledger.repository import sqlite
from scripts.swarm_ledger.repository.sqlite import (
    Missing,
    SQLiteLedgerRepository,
    read_ids,
    read_ledger,
    read_registry,
)

CONTENT = {
    "title": "Store",
    "overview": "o",
    "sources": [],
    "phases": [{"title": "One", "description": "d"}],
    "tasks": [
        {"title": "first", "phase": "p1", "lane": "eng"},
        {"title": "second", "phase": "p1", "lane": "eng"},
    ],
}


def document():
    seed = {
        "title": "Café",
        "tasks": [
            {"id": "second", "proof": {"output": "verified"}, "comments": []},
            {
                "id": "first",
                "unknown": {"a/b": [None, False, 0]},
                "comments": [
                    {"id": "c1", "text": "old", "deleted": True, "extra": [1, 2]},
                ],
            },
        ],
        "questions": [{"id": "q1", "answers": [{"id": "a1", "text": "yes"}]}],
        "artifacts": [{"id": "art", "path": "asset.svg"}],
        "artifact_trash": [{"id": "gone", "deleted_at": 10}],
        "notifications": [],
        "priorities": [],
        "extension": {"nested": {"empty": []}},
    }
    return {
        **seed,
        "_meta": {
            "rev": 1,
            "stamps": {"tasks/first/proof": {"rev": 1}},
            "events": [{"rev": 1, "id": "op", "unknown": {"x": True}}],
            "seeds": {"0": copy.deepcopy({**seed, "title": "Old"}), "1": copy.deepcopy(seed)},
            "members": {"eng": {"handled_rev": 0}},
            "unknown": [3, 4],
        },
    }


def store(tmp_path):
    return SQLiteLedgerRepository(tmp_path / "ledgers" / sqlite.DATABASE)


def writes(statements):
    return [s for s in statements if s.startswith(("INSERT", "UPDATE", "DELETE"))]


def chat(n):
    return {"op": "add", "id": f"m{n}", "thread": "chat", "text": f"message {n}"}


def test_import_exports_the_source_document_and_survives_a_restart(tmp_path):
    repo = store(tmp_path)
    state = document()
    repo.import_document("ledger", state, token="t" * 24)
    assert repo.export_document("ledger") == state
    working = repo.get_document("ledger")
    assert working["_meta"]["seeds"] == {}
    assert {**working, "_meta": {**working["_meta"], "seeds": state["_meta"]["seeds"]}} == state
    restarted = store(tmp_path)
    assert restarted.export_document("ledger") == state
    assert restarted.token("ledger") == "t" * 24
    assert restarted.events_since("ledger", 0) == state["_meta"]["events"]
    assert restarted.events_since("ledger", 1) == []
    with repo.connect() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_import_refuses_an_existing_ledger_unless_replacing(tmp_path):
    repo = store(tmp_path)
    repo.import_document("ledger", document())
    with pytest.raises(ValueError, match="exists"):
        repo.import_document("ledger", document())
    replaced = {**document(), "title": "Replaced"}
    repo.import_document("ledger", replaced, replace=True)
    assert repo.export_document("ledger") == replaced


def test_an_import_that_does_not_export_its_source_stores_nothing(tmp_path, monkeypatch):
    repo = store(tmp_path)
    monkeypatch.setattr(repo, "_export", lambda connection, slug: {"wrong": True})
    with pytest.raises(ValueError, match="does not export"):
        repo.import_document("ledger", document())
    assert not store(tmp_path).exists("ledger")


def test_a_refused_replacing_import_leaves_the_stored_ledger_readable(tmp_path, monkeypatch):
    repo = store(tmp_path)
    repo.create("ledger", CONTENT)
    real = repo._export
    monkeypatch.setattr(repo, "_export", lambda connection, slug: real(connection, slug) and {"wrong": True})
    with pytest.raises(ValueError, match="does not export"):
        repo.import_document("ledger", document(), replace=True)
    assert repo.get_document("ledger")["title"] == "Store"


def test_a_reader_holding_a_cached_copy_sees_a_replace_made_by_another_process(tmp_path, monkeypatch):
    holder, other = store(tmp_path), store(tmp_path)
    holder.create("ledger", CONTENT)
    assert holder.get_document("ledger")["title"] == "Store"
    monkeypatch.setattr(holder.domain, "now_ms", lambda: 1)
    other.import_document("ledger", document(), replace=True)
    assert holder.get_document("ledger")["title"] == "Café"


def test_one_operation_writes_only_the_rows_it_changed_and_no_file(tmp_path):
    repo = store(tmp_path)
    assert repo.create("ledger", CONTENT) is True
    assert repo.create("ledger", CONTENT) is False
    statements = []
    repo.trace = statements.append
    state, rejected = repo.apply_ops("ledger", ops=[chat(1)])
    assert rejected == []
    changed = writes(statements)
    assert 0 < len(changed) < 20
    assert not any("second" in statement for statement in changed)
    assert (
        sorted(path.name for path in (tmp_path / "ledgers").iterdir() if not path.name.startswith(sqlite.DATABASE))
        == []
    )
    assert state["chat"][-1]["text"] == "message 1"


def test_every_write_leaves_rows_and_events_equal_to_the_returned_state(tmp_path, monkeypatch):
    repo = store(tmp_path)
    monkeypatch.setattr(repo.domain, "EVENTS_KEPT", 3)
    repo.create("ledger", CONTENT)
    operations = [
        [chat(1)],
        [{"op": "join", "id": "j1", "by": "eng"}],
        [{"op": "task_update", "id": "u1", "by": "eng", "item": "tasks/t2", "fields": {"state": "claimed"}}],
        [chat(2), chat(3)],
        [{"op": "edit", "id": "m1", "thread": "chat", "text": "edited"}],
        [{"op": "delete", "id": "m2", "thread": "chat"}],
    ]
    for ops in operations:
        state, _ = repo.apply_ops("ledger", ops=ops)
        assert store(tmp_path).get_document("ledger") == state
        assert len(state["_meta"]["events"]) <= 3
        assert store(tmp_path).events_since("ledger", -1) == state["_meta"]["events"]


def test_checkbox_changes_keep_the_stale_base_guard(tmp_path):
    repo = store(tmp_path)
    repo.create("ledger", CONTENT)
    _, rejected = repo.apply_ops("ledger", changes=[{"path": "phases/p1/done", "value": True, "base": True}])
    assert rejected == ["phases/p1/done"]
    state, rejected = repo.apply_ops("ledger", changes=[{"path": "phases/p1/done", "value": True, "base": False}])
    assert rejected == [] and state["phases"][0]["done"] is True


def test_a_failed_mutation_rolls_back_and_the_next_write_starts_clean(tmp_path):
    repo = store(tmp_path)
    repo.create("ledger", CONTENT)
    before = repo.get_document("ledger")

    class Crash:
        def apply(self, doc, op, ctx, apply_op):
            apply_op(doc, op, ctx)
            raise RuntimeError("crash")

    with pytest.raises(RuntimeError, match="crash"):
        repo.apply_ops("ledger", ops=[chat(1)], gate=Crash())
    assert repo.get_document("ledger") == before
    assert store(tmp_path).get_document("ledger") == before
    state, _ = repo.apply_ops("ledger", ops=[chat(2)])
    assert [m["id"] for m in state["chat"]] == ["m2"]


def test_a_second_writer_invalidates_the_first_writers_copy(tmp_path):
    first, second = store(tmp_path), store(tmp_path)
    first.create("ledger", CONTENT)
    first.apply_ops("ledger", ops=[chat(1)])
    second.apply_ops("ledger", ops=[chat(2)])
    state, _ = first.apply_ops("ledger", ops=[chat(3)])
    assert [m["id"] for m in state["chat"]] == ["m1", "m2", "m3"]


def test_a_reader_of_an_older_revision_keeps_the_newer_copy_for_the_next_writer(tmp_path, monkeypatch):
    repo = store(tmp_path)
    repo.create("ledger", CONTENT)
    repo.apply_ops("ledger", ops=[chat(1)])
    with repo.connect() as reader, reader:
        reader.execute("BEGIN")
        reader.execute("SELECT generation FROM ledgers").fetchall()
        repo.apply_ops("ledger", ops=[chat(2)])
        assert [m["id"] for m in json.loads(repo._entry(reader, "ledger").text)["chat"]] == ["m1", "m2"]
    assembled = []
    monkeypatch.setattr(sqlite, "read_rows", lambda *a: assembled.append(a) or pytest.fail("reassembled"))
    state, _ = repo.apply_ops("ledger", ops=[chat(3)])
    assert [m["id"] for m in state["chat"]] == ["m1", "m2", "m3"]
    assert assembled == []


def test_a_refused_write_keeps_the_cached_copy_for_the_next_writer(tmp_path, monkeypatch):
    class Conflict:
        def apply(self, doc, op, ctx, apply_op):
            raise ValueError("revision conflict")

    repo = store(tmp_path)
    repo.create("ledger", CONTENT)
    repo.apply_ops("ledger", ops=[chat(1)])
    with pytest.raises(ValueError, match="revision conflict"):
        repo.apply_ops("ledger", ops=[chat(2)], gate=Conflict())
    monkeypatch.setattr(sqlite, "read_rows", lambda *a: pytest.fail("reassembled"))
    state, _ = repo.apply_ops("ledger", ops=[chat(3)])
    assert [m["id"] for m in state["chat"]] == ["m1", "m3"]


def test_partial_reads_return_only_the_named_parts(tmp_path):
    repo = store(tmp_path)
    repo.create("ledger", CONTENT)
    repo.apply_ops("ledger", ops=[{"op": "join", "id": "j1", "by": "eng"}])
    state = repo.read("ledger", "tasks/t2", "_meta.members", "overview")
    assert set(state) == {"tasks", "_meta", "overview"}
    assert [task["id"] for task in state["tasks"]] == ["t2"]
    assert list(state["_meta"]) == ["members"] and "eng" in state["_meta"]["members"]
    events = read_ledger(tmp_path / "ledgers", "ledger", "_meta.events")["_meta"]["events"]
    assert events == repo.get_document("ledger")["_meta"]["events"]
    assert read_ledger(tmp_path / "ledgers", "ledger", "overview") == {"overview": "o"}
    assert read_ledger(tmp_path / "ledgers", "missing", "overview") is None
    assert read_ledger(tmp_path / "nowhere", "ledger", "overview") is None
    assert read_ids(tmp_path / "ledgers", "ledger", "tasks") == ("t1", "t2")
    assert read_ids(tmp_path / "nowhere", "ledger", "tasks") == ()
    assert read_registry(tmp_path / "ledgers", "bin") == {}
    with pytest.raises(Missing):
        repo.read("missing", "overview")


def test_missing_ledgers_raise_one_error_both_lookup_and_value_callers_catch(tmp_path):
    repo = store(tmp_path)
    for call in (lambda: repo.get_document("missing"), lambda: repo.events_since("missing", 0)):
        with pytest.raises(KeyError):
            call()
        with pytest.raises(ValueError):
            call()
    assert repo.token("missing") is None
    assert repo.exists("missing") is False


def test_summaries_follow_writes_newest_first_and_carry_bin_facts(tmp_path):
    repo = store(tmp_path)
    repo.create("older", CONTENT)
    repo.create("newer", {**CONTENT, "title": "Newer"})
    repo.apply_ops("older", ops=[chat(1)])
    rows = repo.list_summaries()
    assert [row["slug"] for row in rows] == ["older", "newer"]
    assert set(rows[0]) == set(sqlite.SUMMARY_KEYS)
    assert rows[1]["title"] == "Newer" and rows[1]["open"] == 2 and rows[1]["done"] == 0
    full = repo.summaries()[0]
    assert full["finished"] is False and full["created_at"] <= full["updated_at"]
    assert json.loads(json.dumps(rows)) == rows
