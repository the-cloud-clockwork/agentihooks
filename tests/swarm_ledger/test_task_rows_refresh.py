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


def test_rebuilt_gives_up_when_a_changed_task_is_no_longer_stored(tmp_path):
    from scripts.swarm_ledger.repository import refresh
    from scripts.swarm_ledger.repository.rows import encode
    from scripts.swarm_ledger.repository.sqlite import TASKS_PATH, SQLiteLedgerRepository

    path = stored(tmp_path, [{"id": "t0"}])
    gone = {encode(["tasks", ["id", "gone", 0], "title"])}
    with SQLiteLedgerRepository(path).connect() as connection:
        assert refresh.rebuilt(connection, "rows", TASKS_PATH, [{"id": "t0"}], gone) is None


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


def test_task_rows_names_the_missing_ledger(tmp_path):
    from scripts.swarm_ledger.repository.sqlite import Missing, SQLiteLedgerRepository

    path = stored(tmp_path, [])
    with pytest.raises(Missing) as missing:
        SQLiteLedgerRepository(path).task_rows("absent")
    assert missing.value.args == ("absent",)


def test_task_rows_reads_every_task_row_when_the_earlier_rows_do_not_match_storage(tmp_path):
    from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository

    tasks = [{"id": f"t{n}", "title": f"T{n}"} for n in range(3)]
    path = stored(tmp_path, tasks)
    reader = SQLiteLedgerRepository(path)
    since = reader.task_rows("rows")
    changed = [*tasks[:2], {**tasks[2], "title": "Changed"}]
    write_tasks(path, changed)
    assert reader.task_rows("rows", (since[0], since[1], []))[2] == changed


def test_task_rows_reuses_the_untouched_tasks_after_a_task_is_added(tmp_path):
    from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository

    tasks = [{"id": f"t{n}", "notes": {"a": n}} for n in range(3)]
    path = stored(tmp_path, tasks)
    reader = SQLiteLedgerRepository(path)
    since = reader.task_rows("rows")
    added = [*tasks, {"id": "t3", "notes": {}}]
    write_tasks(path, added)
    rows = reader.task_rows("rows", since)[2]
    assert rows == added
    assert [row is old for row, old in zip(rows, since[2])] == [True, True, True]


def test_task_rows_returns_the_earlier_rows_when_no_changed_path_touches_tasks(tmp_path, monkeypatch):
    from scripts.swarm_ledger.repository import refresh
    from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository

    tasks = [{"id": "t0", "title": "T0"}]
    path = stored(tmp_path, tasks)
    reader = SQLiteLedgerRepository(path)
    since = reader.task_rows("rows")
    write_tasks(path, tasks)
    monkeypatch.setattr(refresh, "changed_paths", lambda *args, **kwargs: {"unrelated"})
    assert reader.task_rows("rows", since)[2] is since[2]


def test_rebuilt_replaces_one_changed_item_of_an_array_nested_in_a_task(tmp_path):
    import json

    from scripts.swarm_ledger.repository import refresh
    from scripts.swarm_ledger.repository.rows import encode
    from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository

    items = [{"id": f"i{n}", "text": f"I{n}"} for n in range(3)]
    path = stored(tmp_path, [{"id": "t0", "items": items}])
    since = SQLiteLedgerRepository(path).task_rows("rows")
    changed = [items[0], {**items[1], "text": "Changed"}, items[2]]
    write_tasks(path, [{"id": "t0", "items": changed}])
    parts = ["tasks", ["id", "t0", 0], "items"]
    with SQLiteLedgerRepository(path).connect() as connection:
        paths = refresh.changed_paths(connection, "rows", since[0], since[0] + 1, 256)
        inside = {changed_path for changed_path in paths if json.loads(changed_path)[:3] == parts}
        assert inside and refresh.rebuilt(connection, "rows", encode(parts), items, inside) == changed


def test_task_rows_share_the_cached_entry_rows_at_the_same_generation(tmp_path):
    from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository

    tasks = [{"id": f"t{n}", "title": f"T{n}"} for n in range(3)]
    path = stored(tmp_path, tasks)
    reader = SQLiteLedgerRepository(path)
    since = reader.task_rows("rows")
    write_tasks(path, [*tasks[:2], {**tasks[2], "title": "Changed"}])
    entry = reader.current("rows")
    shared = reader.task_rows("rows", since)
    assert shared[0] == entry.generation == since[0] + 1
    assert shared[2] is entry.state["tasks"]
    assert shared[2] == SQLiteLedgerRepository(path).read("rows", "tasks")["tasks"]
    assert shared[1] == SQLiteLedgerRepository(path).task_rows("rows")[1] != {}


def test_task_rows_read_stored_rows_when_the_cached_entry_is_from_another_generation(tmp_path):
    from scripts.swarm_ledger.repository.entry import Entry
    from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository

    tasks = [{"id": "t0", "title": "T0"}]
    path = stored(tmp_path, tasks)
    reader = SQLiteLedgerRepository(path)
    generation = reader.current("rows").generation
    ahead = [{"id": "ahead"}]
    reader._cache[reader._key("rows")] = Entry(generation + 1, None, {"tasks": ahead})
    rows = reader.task_rows("rows")
    assert rows[0] == generation
    assert rows[2] == tasks and rows[2] is not ahead


def test_task_rows_of_a_cached_ledger_without_tasks_are_an_empty_list(tmp_path):
    from scripts.swarm_ledger.repository.entry import Entry
    from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository

    reader = SQLiteLedgerRepository(stored(tmp_path, []))
    generation = reader.current("rows").generation
    reader._cache[reader._key("rows")] = Entry(generation, None, {"_meta": {}})
    assert reader.task_rows("rows")[2] == []
