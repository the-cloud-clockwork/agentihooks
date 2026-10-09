import itertools
import sqlite3

import pytest

from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger import ledger_tasks
from scripts.swarm_ledger.repository import bin_storage, hierarchy, mutation
from scripts.swarm_ledger.repository import sqlite as store

SLUG = "hierarchy-rows"
IDS = itertools.count()
CONTENT = {
    "title": "Hierarchy",
    "overview": "Intent",
    "sources": [],
    "phases": [{"title": "Build"}, {"title": "Ship", "depends_on": ["p1"]}],
    "tasks": [
        {"title": "first", "phase": "p1", "lane": "eng"},
        {"title": "second", "phase": "p1", "lane": "eng"},
    ],
}
STATE = {
    "plans": [{"id": "a"}],
    "phases": [{"id": "p1", "plan": "plans/a"}, {"id": "p2", "plan": "", "depends_on": ["p1"]}],
    "slices": [{"id": "a.first", "phase": "phases/p1"}],
    "tasks": [
        {"id": "t1", "phase": "p1", "slice": "slices/a.first"},
        {"id": "t2", "phase": "p2", "depends_on": ["t1"]},
        {"id": "t3", "phase": ""},
    ],
}
NODES = {
    "plans/a": ("plan", None, 0),
    "phases/p1": ("phase", "plans/a", 0),
    "phases/p2": ("phase", None, 1),
    "slices/a.first": ("slice", "phases/p1", 0),
    "tasks/t1": ("task", "slices/a.first", 0),
    "tasks/t2": ("task", "phases/p2", 1),
    "tasks/t3": ("task", None, 2),
}
DEPENDENCIES = {("phases/p2", "phases/p1"), ("tasks/t2", "tasks/t1")}
NO_DRIFT = {
    "missing_nodes": [],
    "extra_nodes": [],
    "changed_nodes": [],
    "missing_dependencies": [],
    "extra_dependencies": [],
    "drift": 0,
}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    for name in ledger_tasks.OPS:
        monkeypatch.setitem(core.EXTENSION_OPS, name, ledger_tasks)
    found = store.SQLiteLedgerRepository(tmp_path / store.DATABASE)
    assert found.create(SLUG, CONTENT) is True
    assert run(found, "task_update", item="tasks/t2", fields={"depends_on": ["t1"]})[1] == []
    return found


def rows(repo, slug=SLUG):
    with repo.connect() as connection:
        return hierarchy.stored(connection, slug)


def run(repo, kind, **fields):
    op = {"op": kind, "id": f"{kind}-{next(IDS)}", "by": "planner", **fields}
    core.check_op(op)
    return repo.apply_ops(SLUG, ops=[op])


def test_project_reads_every_node_with_its_parent_position_and_dependencies():
    assert hierarchy.project(STATE) == (NODES, DEPENDENCIES)


def test_project_of_an_empty_document_is_empty():
    assert hierarchy.project({}) == ({}, set())
    assert hierarchy.project({"tasks": None, "phases": []}) == ({}, set())


def test_create_stores_the_tables_of_the_document(repo):
    assert rows(repo) == (
        {
            "phases/p1": ("phase", None, 0),
            "phases/p2": ("phase", None, 1),
            "tasks/t1": ("task", "phases/p1", 0),
            "tasks/t2": ("task", "phases/p1", 1),
        },
        {("phases/p2", "phases/p1"), ("tasks/t2", "tasks/t1")},
    )


def test_foreign_keys_are_on_for_every_connection(repo):
    with repo.connect() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO work_nodes VALUES ('nowhere', 'tasks/x', 'task', NULL, 0)")
    with store.read_only(repo.directory) as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)


def test_the_tables_carry_a_parent_and_a_reverse_dependency_index(repo):
    with repo.connect() as connection:
        indexes = {
            name: [column for _, _, column in connection.execute(f"PRAGMA index_info({name})")]
            for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
    assert indexes["work_nodes_parent"] == ["ledger_slug", "parent_id", "position"]
    assert indexes["work_dependencies_required"] == ["ledger_slug", "requires_id"]


def test_a_task_move_commits_with_the_document(repo):
    state, rejected = run(repo, "task_update", item="tasks/t2", fields={"phase": "p2"})
    assert rejected == []
    assert state["tasks"][1]["phase"] == "p2"
    assert rows(repo)[0]["tasks/t2"] == ("task", "phases/p2", 1)


def test_a_dependency_change_commits_with_the_document(repo):
    assert run(repo, "task_update", item="tasks/t1", fields={"depends_on": ["t2"]})[1] == []
    assert run(repo, "task_update", item="tasks/t2", fields={"depends_on": []})[1] == []
    assert rows(repo)[1] == {("phases/p2", "phases/p1"), ("tasks/t1", "tasks/t2")}


def test_a_delete_commits_with_the_document(repo, monkeypatch):
    real = mutation.apply

    def dropping(slug, state, domain, *args, **kwargs):
        found = real(slug, state, domain, *args, **kwargs)
        state["tasks"] = [task for task in state["tasks"] if task["id"] != "t1"]
        return found

    monkeypatch.setattr(mutation, "apply", dropping)
    state, _ = repo.apply_ops(SLUG, ops=[])
    assert [task["id"] for task in state["tasks"]] == ["t2"]
    assert rows(repo) == (
        {
            "phases/p1": ("phase", None, 0),
            "phases/p2": ("phase", None, 1),
            "tasks/t2": ("task", "phases/p1", 0),
        },
        {("phases/p2", "phases/p1"), ("tasks/t2", "tasks/t1")},
    )


def test_deleting_a_node_drops_its_own_dependencies(repo, monkeypatch):
    real = mutation.apply

    def dropping(slug, state, domain, *args, **kwargs):
        found = real(slug, state, domain, *args, **kwargs)
        state["tasks"] = [task for task in state["tasks"] if task["id"] != "t2"]
        return found

    monkeypatch.setattr(mutation, "apply", dropping)
    repo.apply_ops(SLUG, ops=[])
    assert rows(repo)[1] == {("phases/p2", "phases/p1")}


def test_a_failed_write_rolls_the_rows_back_with_the_document(repo, monkeypatch):
    before = rows(repo)

    def failing(*args):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(store, "append_events", failing)
    with pytest.raises(sqlite3.OperationalError):
        run(repo, "task_update", item="tasks/t2", fields={"phase": "p2", "depends_on": []})
    fresh = store.SQLiteLedgerRepository(repo.path)
    assert fresh.get_document(SLUG)["tasks"][1]["phase"] == "p1"
    assert rows(fresh) == before


def test_a_write_rebuilds_tables_a_ledger_never_had(repo):
    with repo.connect() as connection, connection:
        connection.execute("DELETE FROM work_nodes WHERE ledger_slug=?", (SLUG,))
    assert run(repo, "task_update", item="tasks/t1", fields={"depends_on": ["t2"]})[1] == []
    assert rows(repo) == hierarchy.project(repo.get_document(SLUG))
    assert rows(repo)[1] == {("phases/p2", "phases/p1"), ("tasks/t1", "tasks/t2"), ("tasks/t2", "tasks/t1")}


def test_a_write_repairs_rows_that_drifted_at_the_same_count(repo):
    with repo.connect() as connection, connection:
        connection.execute("DELETE FROM work_nodes WHERE ledger_slug=? AND node_id='tasks/t1'", (SLUG,))
        connection.execute("INSERT INTO work_nodes VALUES (?, 'tasks/ghost', 'task', NULL, 9)", (SLUG,))
    assert run(repo, "task_update", item="tasks/t1", fields={"depends_on": ["t2"]})[1] == []
    assert rows(repo) == hierarchy.project(repo.get_document(SLUG))


def test_restore_keeps_the_rows_and_purge_removes_them(repo, monkeypatch):
    monkeypatch.setattr(bin_storage, "_repository", lambda: repo)
    before = rows(repo)
    bin_storage.delete(SLUG, now=1)
    assert rows(repo) == before
    assert bin_storage.restore(SLUG, now=2) is True
    assert rows(repo) == before
    with repo.connect() as connection, connection:
        connection.execute(store.BEGIN_IMMEDIATE)
        repo.purge(SLUG, connection)
    assert rows(repo) == ({}, set())


def test_import_and_export_keep_the_rows(repo, tmp_path):
    exported = repo.export_document(SLUG)
    copy = store.SQLiteLedgerRepository(tmp_path / "copy" / store.DATABASE)
    copy.import_document(SLUG, exported)
    assert copy.export_document(SLUG) == exported
    assert rows(copy) == rows(repo)
    assert copy.rebuild(SLUG) == NO_DRIFT


def test_a_replacing_import_replaces_the_rows(repo):
    exported = repo.export_document(SLUG)
    exported["tasks"] = exported["tasks"][:1]
    repo.import_document(SLUG, exported, replace=True)
    assert rows(repo) == (
        {"phases/p1": ("phase", None, 0), "phases/p2": ("phase", None, 1), "tasks/t1": ("task", "phases/p1", 0)},
        {("phases/p2", "phases/p1")},
    )


def test_rebuild_after_writes_reports_zero_drift(repo):
    run(repo, "task_update", item="tasks/t2", fields={"phase": "p2", "depends_on": []})
    assert repo.rebuild(SLUG) == NO_DRIFT


def test_rebuild_on_a_fresh_copy_reports_zero_drift(repo, tmp_path):
    copy = store.SQLiteLedgerRepository(tmp_path / "fresh" / store.DATABASE)
    copy.import_document(SLUG, repo.export_document(SLUG))
    assert copy.rebuild(SLUG) == NO_DRIFT
    assert rows(copy) == hierarchy.project(repo.get_document(SLUG))


def test_rebuild_reports_and_repairs_drift(repo):
    with repo.connect() as connection, connection:
        connection.execute("UPDATE work_nodes SET parent_id='phases/p2' WHERE node_id='tasks/t1'")
        connection.execute("DELETE FROM work_nodes WHERE node_id='phases/p2'")
        connection.execute("INSERT INTO work_nodes VALUES (?, 'tasks/ghost', 'task', NULL, 9)", (SLUG,))
        connection.execute("INSERT INTO work_dependencies VALUES (?, 'tasks/t1', 'tasks/ghost')", (SLUG,))
        connection.execute("DELETE FROM work_dependencies WHERE node_id='tasks/t2'")
    assert repo.rebuild(SLUG) == {
        "missing_nodes": ["phases/p2"],
        "extra_nodes": ["tasks/ghost"],
        "changed_nodes": ["tasks/t1"],
        "missing_dependencies": [["phases/p2", "phases/p1"], ["tasks/t2", "tasks/t1"]],
        "extra_dependencies": [["tasks/t1", "tasks/ghost"]],
        "drift": 6,
    }
    assert repo.rebuild(SLUG) == NO_DRIFT
    assert rows(repo) == hierarchy.project(repo.get_document(SLUG))


def test_rebuild_of_an_unknown_ledger_is_refused(repo):
    with pytest.raises(store.Missing):
        repo.rebuild("nowhere")


def test_rows_of_one_ledger_leave_another_alone(repo):
    assert repo.create("other", {**CONTENT, "tasks": []}) is True
    assert rows(repo, "other") == (
        {"phases/p1": ("phase", None, 0), "phases/p2": ("phase", None, 1)},
        {("phases/p2", "phases/p1")},
    )
    run(repo, "task_update", item="tasks/t2", fields={"phase": "p2"})
    assert rows(repo, "other")[0] == {"phases/p1": ("phase", None, 0), "phases/p2": ("phase", None, 1)}
    assert repo.rebuild("other") == NO_DRIFT
