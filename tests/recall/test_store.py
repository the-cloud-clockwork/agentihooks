import sqlite3
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.recall.models import RecallRecord
from scripts.recall.store import RecallStore, SQLiteRecallStore, SyncCounts, default_path


def record(ref, text="body", index=0, **fields):
    values = dict(
        key=f"demo/{ref}#{index}",
        ledger_slug="demo",
        swarm_slug="swarm",
        kind="task",
        ref=ref,
        parent_ref="phases/p1",
        author="engineer@1",
        time=1000,
        title=f"title of {ref}",
        text=text,
        chunk_index=index,
    )
    values.update(fields)
    return RecallRecord(**values)


@pytest.fixture
def store(tmp_path):
    return SQLiteRecallStore(tmp_path / "recall" / "recall.sqlite3")


def rows(store):
    with sqlite3.connect(store.path) as connection:
        return {
            key: (state, digest, parent)
            for key, state, digest, parent in connection.execute(
                "SELECT key, source_state, content_hash, parent_ref FROM records"
            )
        }


def test_default_path_sits_under_the_agentihooks_home(tmp_path):
    assert default_path({"AGENTIHOOKS_HOME": str(tmp_path)}) == tmp_path / "recall" / "recall.sqlite3"
    assert default_path({}) == Path.home() / ".agentihooks" / "recall" / "recall.sqlite3"


def test_the_sqlite_store_satisfies_the_protocol(store):
    assert isinstance(store, RecallStore)


def test_the_database_runs_in_wal_mode_with_a_busy_timeout(store):
    with store.connect() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert connection.execute("PRAGMA busy_timeout").fetchone() == (30000,)


def test_upsert_inserts_new_records_and_rewrites_changed_ones(store):
    assert store.sync("ledger/demo", [record("tasks/t1"), record("tasks/t2")]) == SyncCounts(written=2)
    assert store.sync("ledger/demo", [record("tasks/t1", text="edited words"), record("tasks/t2")]) == SyncCounts(
        written=1, unchanged=1
    )
    assert store.match("edited") == ["demo/tasks/t1#0"]
    assert store.match("body") == ["demo/tasks/t2#0"]
    assert set(rows(store)) == {"demo/tasks/t1#0", "demo/tasks/t2#0"}


def test_an_unchanged_hash_is_not_rewritten(store):
    store.sync("ledger/demo", [record("tasks/t1")])
    before = rows(store)
    with store.connect() as watcher:
        version = watcher.execute("PRAGMA data_version").fetchone()
        assert store.sync("ledger/demo", [record("tasks/t1")]) == SyncCounts(unchanged=1)
        assert watcher.execute("PRAGMA data_version").fetchone() == version
        store.sync("ledger/demo", [record("tasks/t1", text="changed")])
        assert watcher.execute("PRAGMA data_version").fetchone() != version
    assert rows(store)["demo/tasks/t1#0"] != before["demo/tasks/t1#0"]


def test_every_field_feeds_the_content_hash(store):
    base = record("tasks/t1")
    store.sync("ledger/demo", [base])
    for field, value in [
        ("swarm_slug", "other"),
        ("kind", "note"),
        ("parent_ref", "ledger"),
        ("author", "operator"),
        ("time", 2000),
        ("title", "renamed"),
        ("text", "new"),
    ]:
        assert store.sync("ledger/demo", [replace(base, **{field: value})]).written == 1, field
        store.sync("ledger/demo", [base])


def test_a_record_dropped_by_retention_stays_archived_and_searchable(store):
    store.sync("ledger/demo", [record("chat/m1", text="ancient words", kind="chat"), record("chat/m2", kind="chat")])
    assert store.sync("ledger/demo", [record("chat/m2", kind="chat")]) == SyncCounts(unchanged=1, archived=1)
    assert store.match("ancient") == ["demo/chat/m1#0"]
    assert rows(store)["demo/chat/m1#0"][0] == "archived"
    assert rows(store)["demo/chat/m2#0"][0] == "present"
    assert store.sync("ledger/demo", [record("chat/m2", kind="chat")]) == SyncCounts(unchanged=1)


def test_an_archived_record_seen_again_is_present(store):
    store.sync("ledger/demo", [record("chat/m1")])
    store.sync("ledger/demo", [])
    assert store.sync("ledger/demo", [record("chat/m1")]) == SyncCounts(written=1)
    assert rows(store)["demo/chat/m1#0"][0] == "present"


def test_sync_archives_only_its_own_source(store):
    store.sync("ledger/demo", [record("tasks/t1")])
    store.sync("ledger/other", [record("tasks/t9", key="other/tasks/t9#0", ledger_slug="other")])
    store.sync("ledger/demo", [])
    assert rows(store)["other/tasks/t9#0"][0] == "present"


def test_a_shrunken_item_drops_its_trailing_chunks(store):
    store.sync("ledger/demo", [record("tasks/t1", text=f"part{i}", index=i) for i in range(3)])
    counts = store.sync("ledger/demo", [record("tasks/t1", text="part0")])
    assert counts == SyncCounts(unchanged=1, removed=2)
    assert set(rows(store)) == {"demo/tasks/t1#0"}
    assert store.match("part2") == []


def test_a_deleted_source_entry_is_removed_with_its_children(store):
    store.sync(
        "ledger/demo",
        [
            record("tasks/t1", text="gone"),
            record("tasks/t1/comments/c1", text="gone child", kind="comment", parent_ref="tasks/t1"),
            record("tasks/t10", text="kept"),
        ],
    )
    assert store.remove("ledger/demo", ["tasks/t1"]) == 2
    assert set(rows(store)) == {"demo/tasks/t10#0"}
    assert store.match("gone") == []
    assert store.remove("ledger/demo", []) == 0


def test_remove_leaves_other_sources_alone(store):
    store.sync("ledger/other", [record("tasks/t1", key="other/tasks/t1#0", ledger_slug="other")])
    assert store.remove("ledger/demo", ["tasks/t1"]) == 0
    assert set(rows(store)) == {"other/tasks/t1#0"}


def test_ids_with_hyphens_and_underscores_stay_whole_tokens(store):
    store.sync("ledger/demo", [record("tasks/t1", text="claimed by engineer-323133-0630 on swarm_v2")])
    assert store.match('"engineer-323133-0630"') == ["demo/tasks/t1#0"]
    assert store.match('"swarm_v2"') == ["demo/tasks/t1#0"]
    assert store.match("323133") == []
    assert store.match("swarm") == []


def test_title_is_searchable(store):
    store.sync("ledger/demo", [record("tasks/t1", title="Recall archive")])
    assert store.match("archive") == ["demo/tasks/t1#0"]


def test_two_writers_on_separate_connections_both_land(tmp_path):
    path = tmp_path / "recall.sqlite3"
    SQLiteRecallStore(path)
    errors = []

    def write(slug):
        writer = SQLiteRecallStore(path)
        try:
            for round_ in range(40):
                batch = [
                    record(f"tasks/t{i}", text=f"{slug} round{round_}", key=f"{slug}/tasks/t{i}#0", ledger_slug=slug)
                    for i in range(20)
                ]
                writer.sync(f"ledger/{slug}", batch)
        except sqlite3.Error as error:
            errors.append(error)

    threads = [threading.Thread(target=write, args=(slug,)) for slug in ("one", "two")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    final = SQLiteRecallStore(path)
    assert len(final.match("round39")) == 40
    assert len(rows(final)) == 40
