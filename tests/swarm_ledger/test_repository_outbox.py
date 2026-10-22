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
    with repo.connect() as connection, connection:
        connection.execute("DELETE FROM outbox")
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


def test_creating_a_ledger_writes_one_outbox_row_per_task_and_phase(tmp_path):
    repo = SQLiteLedgerRepository(tmp_path / "ledgers" / sqlite.DATABASE)
    repo.create("ledger", CONTENT)
    state = repo.get_document("ledger")
    assert [(row[2], row[3]) for row in rows(repo)] == [
        (state["_meta"]["rev"], "swarm.ledger.phase.p1"),
        (state["_meta"]["rev"], "swarm.ledger.task.t1"),
        (state["_meta"]["rev"], "swarm.ledger.task.t2"),
    ]
    assert [event.data for event in events(repo)] == [state["phases"][0], *state["tasks"]]


def test_a_replacing_import_writes_rows_for_the_tasks_and_phases_it_changes_and_removes(tmp_path):
    repo = store(tmp_path)
    source = repo.get_document("ledger")
    replacement = {**source, "tasks": [{**source["tasks"][0], "title": "renamed"}]}
    repo.import_document("ledger", replacement, replace=True)
    assert [(row[3], event.type, event.data) for row, event in zip(rows(repo), events(repo))] == [
        ("swarm.ledger.task.t1", "task.changed", replacement["tasks"][0]),
        ("swarm.ledger.task.t2", "task.removed", {}),
    ]


def test_purging_a_ledger_writes_a_removal_row_per_task_and_phase(tmp_path):
    repo = store(tmp_path)
    revision = repo.get_document("ledger")["_meta"]["rev"]
    with repo.transaction() as transaction:
        transaction.purge("ledger")
    assert [(row[2], row[3], event.type, event.data) for row, event in zip(rows(repo), events(repo))] == [
        (revision, "swarm.ledger.phase.p1", "phase.removed", {}),
        (revision, "swarm.ledger.task.t1", "task.removed", {}),
        (revision, "swarm.ledger.task.t2", "task.removed", {}),
    ]


def test_purging_a_slug_with_no_ledger_writes_no_outbox_row(tmp_path):
    repo = store(tmp_path)
    with repo.transaction() as transaction:
        transaction.purge("absent")
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


def test_changed_orders_many_keys_by_key_under_any_hash_seed():
    keys = [f"t{n}" for n in (9, 3, 7, 1, 8, 2, 6, 0, 5, 4)]
    found = outbox.changed({}, {"tasks": [{"id": key} for key in keys]})
    assert [key for _, key, _ in found] == sorted(keys)


def test_a_removed_item_is_written_as_a_removal_with_empty_data():
    connection = sqlite3.connect(":memory:")
    connection.executescript(sqlite.SCHEMA)
    outbox.write(connection, "demo", 7, {"tasks": [{"id": "t1"}]}, {"tasks": []})
    [(slug, revision, subject, value)] = connection.execute("SELECT slug,revision,subject,value FROM outbox")
    event = Envelope.decode(value.encode())
    assert (slug, revision, subject) == ("demo", 7, "swarm.demo.task.t1")
    assert (event.type, event.entity, event.key, event.version, event.data) == ("task.removed", "task", "t1", 7, {})
    assert json.loads(value)["swarm"] == "demo"


def test_a_key_with_subject_reserved_characters_is_escaped_in_the_subject_and_kept_whole_in_the_event():
    connection = sqlite3.connect(":memory:")
    connection.executescript(sqlite.SCHEMA)
    key = "<id> a.b*%"
    outbox.write(connection, "demo", 3, {}, {"tasks": [{"id": key}]})
    [(subject, value)] = connection.execute("SELECT subject,value FROM outbox")
    assert subject == "swarm.demo.task.<id%3E%20a%2Eb%2A%25"
    assert Envelope.decode(value.encode()).key == key
    assert outbox.token("t-admit-headroom") == "t-admit-headroom"


def test_a_wide_whitespace_key_escapes_to_its_utf8_bytes_and_never_meets_another_key():
    assert outbox.token(" ") == "%E2%80%80"
    assert outbox.token(" 00") == "%2000"


def test_the_postgres_schema_and_statement_carry_the_outbox():
    assert "CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY" in sqlite.SCHEMA
    assert (
        'CREATE TABLE IF NOT EXISTS outbox (id TEXT COLLATE "C" PRIMARY KEY, slug TEXT COLLATE "C" NOT NULL, '
        'revision BIGINT NOT NULL, subject TEXT COLLATE "C" NOT NULL, value TEXT COLLATE "C" NOT NULL)'
    ) in postgres.ddl(sqlite.SCHEMA)
    assert postgres.translate(outbox.INSERT) == "INSERT INTO outbox VALUES (%s, %s, %s, %s, %s)"
