import json
import sqlite3

import pytest

from scripts.events.envelope import Envelope
from scripts.swarm_ledger.repository import outbox, postgres, sqlite
from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository

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
CLAIM = {"op": "task_update", "id": "u1", "by": "eng", "item": "tasks/t2", "fields": {"state": "claimed"}}
CHAT = {"op": "add", "id": "m1", "thread": "chat", "text": "hello"}


def store(tmp_path):
    repo = SQLiteLedgerRepository(tmp_path / "ledgers" / sqlite.DATABASE)
    repo.create("ledger", CONTENT)
    return repo


def rows(repo):
    with repo.connect() as connection:
        return connection.execute(
            "SELECT id,slug,revision,subject,value FROM outbox ORDER BY revision,subject"
        ).fetchall()


def events(repo):
    return [Envelope.decode(value.encode()) for *_, value in rows(repo)]


def test_a_task_change_writes_one_outbox_row_carrying_the_changed_task(tmp_path):
    repo = store(tmp_path)
    assert rows(repo) == []
    state, _ = repo.apply_ops("ledger", ops=[CLAIM])
    [(row_id, slug, revision, subject, _)] = rows(repo)
    [event] = events(repo)
    task = next(task for task in state["tasks"] if task["id"] == "t2")
    assert (row_id, slug, revision, subject) == (event.id, "ledger", state["_meta"]["rev"], "swarm.ledger.task.t2")
    assert (event.type, event.swarm, event.entity, event.key) == ("task.changed", "ledger", "task", "t2")
    assert (event.version, event.author, event.cause, event.data) == (state["_meta"]["rev"], "ledger", None, task)


def test_a_phase_change_writes_one_outbox_row_carrying_the_changed_phase(tmp_path):
    repo = store(tmp_path)
    state, _ = repo.apply_ops("ledger", changes=[{"path": "phases/p1/done", "value": True, "base": False}])
    [event] = events(repo)
    assert (event.type, event.entity, event.key, event.version) == (
        "phase.changed",
        "phase",
        "p1",
        state["_meta"]["rev"],
    )
    assert event.data == state["phases"][0] and event.data["done"] is True
    assert rows(repo)[0][3] == "swarm.ledger.phase.p1"


def test_a_change_to_neither_tasks_nor_phases_writes_no_outbox_row(tmp_path):
    repo = store(tmp_path)
    state, _ = repo.apply_ops("ledger", ops=[CHAT])
    assert [message["id"] for message in state["chat"]] == ["m1"]
    assert rows(repo) == []


def test_a_ledger_write_that_fails_after_the_outbox_row_leaves_neither(tmp_path, monkeypatch):
    repo = store(tmp_path)
    before = repo.get_document("ledger")

    def broken(slug, state):
        raise RuntimeError("summary failed")

    monkeypatch.setattr(sqlite, "summarize", broken)
    with pytest.raises(RuntimeError) as caught:
        repo.apply_ops("ledger", ops=[CLAIM])
    assert str(caught.value) == "summary failed"
    monkeypatch.undo()
    assert rows(repo) == []
    assert SQLiteLedgerRepository(repo.path).get_document("ledger") == before


def test_an_outbox_row_that_cannot_be_written_leaves_the_ledger_unchanged(tmp_path, monkeypatch):
    repo = store(tmp_path)
    before = repo.get_document("ledger")
    monkeypatch.setattr(outbox, "INSERT", "INSERT INTO missing VALUES (?, ?, ?, ?, ?)")
    with pytest.raises(sqlite3.OperationalError) as caught:
        repo.apply_ops("ledger", ops=[CLAIM])
    assert str(caught.value) == "no such table: missing"
    monkeypatch.undo()
    assert rows(repo) == []
    assert SQLiteLedgerRepository(repo.path).get_document("ledger") == before


def test_changed_names_added_changed_and_removed_items_of_both_collections_in_key_order():
    before = {"tasks": [{"id": "t1", "a": 1}, {"id": "t2", "a": 1}, {"id": "t3"}], "phases": [{"id": "p1"}]}
    after = {"tasks": [{"id": "t4"}, {"id": "t2", "a": 2}, {"id": "t1", "a": 1}], "phases": [], "chat": [1]}
    assert outbox.changed(before, after) == [
        ("task", "t2", {"id": "t2", "a": 2}),
        ("task", "t3", None),
        ("task", "t4", {"id": "t4"}),
        ("phase", "p1", None),
    ]
    assert outbox.changed({}, {}) == []


def test_a_removed_item_is_written_as_a_removal_with_empty_data():
    connection = sqlite3.connect(":memory:")
    connection.executescript(sqlite.SCHEMA)
    outbox.write(connection, "demo", 7, {"tasks": [{"id": "t1"}]}, {"tasks": []})
    [(slug, revision, subject, value)] = connection.execute("SELECT slug,revision,subject,value FROM outbox")
    event = Envelope.decode(value.encode())
    assert (slug, revision, subject) == ("demo", 7, "swarm.demo.task.t1")
    assert (event.type, event.entity, event.key, event.version, event.data) == ("task.removed", "task", "t1", 7, {})
    assert json.loads(value)["swarm"] == "demo"


def test_the_postgres_schema_and_statement_carry_the_outbox():
    assert "CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY" in sqlite.SCHEMA
    assert (
        'CREATE TABLE IF NOT EXISTS outbox (id TEXT COLLATE "C" PRIMARY KEY, slug TEXT COLLATE "C" NOT NULL, '
        'revision BIGINT NOT NULL, subject TEXT COLLATE "C" NOT NULL, value TEXT COLLATE "C" NOT NULL)'
    ) in postgres.ddl(sqlite.SCHEMA)
    assert postgres.translate(outbox.INSERT) == "INSERT INTO outbox VALUES (%s, %s, %s, %s, %s)"
