import pytest

pytestmark = pytest.mark.unit


def stored(tmp_path, tasks: list):
    from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository

    path = tmp_path / "ledger.sqlite3"
    SQLiteLedgerRepository(path).import_document("rows", {"title": "Rows", "tasks": tasks, "_meta": {"rev": 3}})
    return path


def write_tasks(path, tasks: list) -> None:
    from scripts.swarm_ledger.repository.sqlite import BEGIN_IMMEDIATE, SQLiteLedgerRepository

    writer = SQLiteLedgerRepository(path)
    entry = writer.current("rows")
    with writer.connect() as connection, connection:
        connection.execute(BEGIN_IMMEDIATE)
        writer._write(connection, "rows", entry, {**entry.mutable(), "tasks": tasks}, [])


@pytest.mark.parametrize(
    "old,position,identity,same",
    [
        ([{"id": "a"}], 0, ["id", "a", 0], True),
        ([{"id": "a"}], 0, ["id", "b", 0], False),
        ([{"id": "a"}], 0, ["id", "a", 1], False),
        ([{"id": "a"}], 1, ["id", "a", 0], False),
        ([{"id": "a"}], 0, ["index", 0, 0], False),
        ([["a"]], 0, ["id", "a", 0], False),
        ([{"title": "a"}], 0, ["id", "a", 0], False),
    ],
)
def test_same_child_matches_only_the_first_old_task_with_that_id(old, position, identity, same):
    from scripts.swarm_ledger.repository import refresh

    assert refresh.same_child(old, position, identity) is same


def test_rebuilt_replaces_only_the_changed_task_and_reuses_the_others(tmp_path):
    from scripts.swarm_ledger.repository import refresh
    from scripts.swarm_ledger.repository.sqlite import TASKS_PATH, TASKS_PREFIX, SQLiteLedgerRepository

    tasks = [{"id": f"t{n}", "title": f"T{n}", "notes": {"a": n}, "comments": [{"id": "c"}]} for n in range(4)]
    path = stored(tmp_path, tasks)
    reader = SQLiteLedgerRepository(path)
    since = reader.task_rows("rows")
    write_tasks(path, [*tasks[:2], {**tasks[2], "title": "Changed"}, tasks[3]])
    with reader.connect() as connection:
        paths = refresh.changed_paths(connection, "rows", since[0], since[0] + 1, 256)
        tasks_changed = {path for path in paths if path.startswith(TASKS_PREFIX)}
        rows = refresh.rebuilt(connection, "rows", TASKS_PATH, since[2], tasks_changed)
    assert rows is not None and rows is not since[2]
    assert rows == SQLiteLedgerRepository(path).read("rows", "tasks")["tasks"]
    assert [row is old for row, old in zip(rows, since[2])] == [True, True, False, True]
    assert rows[2]["notes"] is since[2][2]["notes"]


def test_rebuilt_gives_up_when_the_tasks_array_itself_changed(tmp_path):
    from scripts.swarm_ledger.repository import refresh
    from scripts.swarm_ledger.repository.sqlite import TASKS_PATH, SQLiteLedgerRepository

    path = stored(tmp_path, [{"id": "t0"}])

    with SQLiteLedgerRepository(path).connect() as connection:
        assert refresh.rebuilt(connection, "rows", TASKS_PATH, [{"id": "t0"}], {TASKS_PATH}) is None


def test_task_list_pages_and_ledgers_stay_within_their_caps(tmp_path, monkeypatch):
    from scripts.swarm_ledger.api import task_list
    from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository

    path = stored(tmp_path, [{"id": f"t{n}"} for n in range(5)])

    repository = SQLiteLedgerRepository(path)
    for slug in ("one", "two", "three"):
        repository.import_document(slug, {"title": slug, "tasks": [{"id": "t"}], "_meta": {"rev": 1}})
    monkeypatch.setattr(task_list, "HELD", {})
    monkeypatch.setattr(task_list, "PAGES_KEPT", 2)
    monkeypatch.setattr(task_list, "LEDGERS_KEPT", 2)
    for limit in (1, 2, 3):
        task_list.read(repository, "rows", {"limit": limit})
        assert len(task_list.HELD["rows"].pages) <= 2
    for slug in ("one", "two", "three"):
        task_list.read(repository, slug, {})
    assert list(task_list.HELD) == ["two", "three"]
    assert task_list.HELD["three"].pages


def test_task_rows_adopts_only_the_legacy_ledger_it_reads(tmp_path):
    import json

    from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository

    for name in ("alpha", "beta"):
        document = {"title": name, "tasks": [{"id": "t1", "title": name}], "_meta": {"rev": 1}}
        (tmp_path / f"{name}.json").write_text(json.dumps(document))
    repository = SQLiteLedgerRepository(tmp_path / "ledgers.sqlite3")
    assert [row["title"] for row in repository.task_rows("alpha")[2]] == ["alpha"]
    assert (tmp_path / "beta.json").exists() and not (tmp_path / "alpha.json").exists()
