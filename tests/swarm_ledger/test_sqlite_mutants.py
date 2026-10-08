import json
import sqlite3
from pathlib import Path

import ledger_core
import new_ledger
import pytest

from scripts.swarm_ledger.repository import bin_storage, legacy, sqlite
from scripts.swarm_ledger.repository.rows import encode
from scripts.swarm_ledger.repository.sqlite import (
    Entry,
    SQLiteLedgerRepository,
    read_registry,
    scope,
    summarize,
)
from tests.swarm_ledger.test_sqlite import CONTENT, document, store


def test_summarize_counts_done_filters_out_of_scope_and_wires_helpers():
    state = {
        "title": "Store",
        "overview": "hello world",
        "size": "small",
        "closed_at": 99,
        "tasks": [
            {"done": True},
            {"done": False},
            {"out_of_scope": True, "done": True},
        ],
        "_meta": {"updated_at": 10, "created_at": 5},
    }
    assert summarize("ledger", state) == {
        "slug": "ledger",
        "title": "Store",
        "overview": "hello world",
        "closed_at": 99,
        "size": "small",
        "open": 1,
        "done": 1,
        "updated_at": 10,
        "created_at": 5,
        "finished": False,
    }


def test_summarize_falls_back_to_phases_title_and_defaults_when_tasks_are_absent():
    state = {"phases": [{"done": True}, {"done": True}]}
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


def test_scope_lists_every_ancestor_path_and_the_descendant_prefix():
    exact, prefix = scope(["tasks", "t1"])
    assert exact == [encode([]), encode(["tasks"]), encode(["tasks", "t1"])]
    assert prefix == encode(["tasks", "t1"])[:-1] + ","
    only_exact, only_prefix = scope([])
    assert only_exact == [encode([])]
    assert only_prefix == encode([])[:-1] + ","


def test_init_sets_the_explicit_path_and_default_domain_cache_and_guard():
    explicit = SQLiteLedgerRepository(Path("some/ledgers.sqlite3"))
    assert explicit._path == Path("some/ledgers.sqlite3")

    sentinel = object()
    bare = SQLiteLedgerRepository(domain=sentinel)
    assert bare._path is None
    assert bare._domain is sentinel
    assert bare.trace is None
    assert bare._ready == set()
    assert bare._cache == {}
    with bare._guard:
        pass


def test_bound_returns_self_when_path_is_fixed_or_domain_already_matches():
    fixed = SQLiteLedgerRepository(Path("ledger.db"))
    other_domain = object()
    assert fixed.bound(other_domain) is fixed

    flexible = SQLiteLedgerRepository()
    assert flexible.bound(flexible.domain) is flexible


def test_bound_shares_cache_ready_and_guard_with_a_twin_for_another_domain():
    flexible = SQLiteLedgerRepository()
    other_domain = object()
    twin = flexible.bound(other_domain)
    assert twin is not flexible
    assert twin._domain is other_domain
    assert twin._cache is flexible._cache
    assert twin._ready is flexible._ready
    assert twin._guard is flexible._guard


def test_key_pairs_the_database_path_with_the_slug(tmp_path):
    r = store(tmp_path)
    assert r._key("ledger") == (str(r.path), "ledger")


def test_adopt_forwards_self_and_slug_to_legacy_adopt(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(legacy, "adopt", lambda repo, slug: calls.append((repo, slug)))
    r = store(tmp_path)
    r._adopt("ledger-x")
    assert calls == [(r, "ledger-x")]


@pytest.mark.parametrize(
    "call",
    [
        lambda r: r.exists("ledger"),
        lambda r: r.get_document("ledger"),
        lambda r: r.read("ledger", "title"),
        lambda r: r.token("ledger"),
        lambda r: r.events_since("ledger", 0),
        lambda r: r.export_document("ledger"),
    ],
)
def test_each_reader_adopts_its_own_slug_before_touching_the_database(tmp_path, monkeypatch, call):
    r = store(tmp_path)
    r.create("ledger", CONTENT)
    seen = []
    monkeypatch.setattr(r, "_adopt", lambda slug: seen.append(slug))
    call(r)
    assert seen == ["ledger"]


def test_exists_is_false_before_creation_and_true_after(tmp_path):
    r = store(tmp_path)
    assert r.exists("ledger") is False
    r.create("ledger", CONTENT)
    assert r.exists("ledger") is True


def test_entry_reads_fresh_when_nothing_is_cached_and_traces_the_select(tmp_path, monkeypatch):
    r = store(tmp_path)
    monkeypatch.setattr(r.domain, "now_ms", lambda: 1000)
    r.create("ledger", CONTENT)
    statements = []
    r.trace = statements.append
    with r.connect() as connection:
        entry = r._entry(connection, "ledger")
    assert entry.generation == 1000 << 20
    assert entry.state["title"] == CONTENT["title"]
    assert statements[0].startswith("SELECT generation FROM ledgers WHERE slug=")
    assert r._cache[r._key("ledger")] is entry


def test_entry_returns_the_cached_entry_when_generations_match(tmp_path):
    r = store(tmp_path)
    r.create("ledger", CONTENT)
    with r.connect() as connection:
        first = r._entry(connection, "ledger")
    statements = []
    r.trace = statements.append
    with r.connect() as connection:
        second = r._entry(connection, "ledger")
    assert second is first
    assert len(statements) == 1
    assert statements[0].startswith("SELECT generation FROM ledgers WHERE slug=")


def test_entry_rereads_when_the_cache_predates_the_stored_generation(tmp_path):
    r = store(tmp_path)
    r.create("ledger", CONTENT)
    with r.connect() as connection:
        stale = r._entry(connection, "ledger")
    r._cache[r._key("ledger")] = Entry(stale.generation - 1, stale.text, stale.state)
    with r.connect() as connection:
        fresh = r._entry(connection, "ledger")
    assert fresh is not stale
    assert fresh.generation == stale.generation


def test_entry_keeps_a_stale_readers_cache_when_not_asking_for_latest(tmp_path):
    r = store(tmp_path)
    r.create("ledger", CONTENT)
    with r.connect() as connection:
        base = r._entry(connection, "ledger")
    ahead = Entry(base.generation + 5, base.text, base.state)
    r._cache[r._key("ledger")] = ahead
    with r.connect() as connection:
        kept = r._entry(connection, "ledger")
    assert kept is ahead


def test_remember_stores_the_first_entry_for_a_slug(tmp_path):
    r = store(tmp_path)
    entry = Entry(3, "text", {})
    r._remember("ledger", entry)
    assert r._cache[r._key("ledger")] is entry


def test_remember_only_overwrites_with_a_strictly_newer_generation(tmp_path):
    r = store(tmp_path)
    current = Entry(5, "text-5", {})
    r._remember("ledger", current)
    same = Entry(5, "text-5b", {})
    r._remember("ledger", same)
    assert r._cache[r._key("ledger")] is current
    older = Entry(3, "text-3", {})
    r._remember("ledger", older)
    assert r._cache[r._key("ledger")] is current
    newer = Entry(6, "text-6", {})
    r._remember("ledger", newer)
    assert r._cache[r._key("ledger")] is newer


def test_write_advances_the_generation_updates_the_row_and_appends_events(tmp_path, monkeypatch):
    r = store(tmp_path)
    r.create("ledger", CONTENT)
    monkeypatch.setattr(r.domain, "now_ms", lambda: 777777)
    with r.connect() as connection:
        entry = r._entry(connection, "ledger")
    new_state = json.loads(entry.text)
    new_state["title"] = "Renamed"
    statements = []
    r.trace = statements.append
    with r.connect() as connection, connection:
        written = r._write(connection, "ledger", entry, new_state, [{"id": "e1", "rev": 1}])
    assert written.generation == entry.generation + 1
    assert written.state["title"] == "Renamed"
    assert written.text == encode(new_state)
    assert any(s.startswith("UPDATE ledgers SET revision=") for s in statements)
    with r.connect() as connection:
        row = connection.execute(
            "SELECT revision, generation, touched_at FROM ledgers WHERE slug=?", ("ledger",)
        ).fetchone()
    assert row == (new_state["_meta"]["rev"], entry.generation + 1, 777777)
    assert r.events_since("ledger", -1) == [{"id": "e1", "rev": 1}]
    assert r.get_document("ledger")["title"] == "Renamed"


def test_insert_writes_the_row_seeds_and_events_then_overwrites_and_drops_the_cache(tmp_path, monkeypatch):
    r = store(tmp_path)
    monkeypatch.setattr(r.domain, "now_ms", lambda: 123456)
    state = document()
    seeds = state["_meta"]["seeds"]
    stored = {**state, "_meta": {**state["_meta"], "seeds": {}}}
    with r.connect() as connection, connection:
        r._insert(connection, "ledger", stored, "token-a", seeds)
    with r.connect() as connection:
        row = connection.execute(
            "SELECT revision, generation, token, touched_at FROM ledgers WHERE slug=?", ("ledger",)
        ).fetchone()
    assert row == (state["_meta"]["rev"], 123456 << 20, "token-a", 123456)
    assert r.events_since("ledger", -1) == state["_meta"]["events"]
    assert r.export_document("ledger")["_meta"]["seeds"] == seeds

    r._cache[r._key("ledger")] = Entry(999999, "stale", {})
    with r.connect() as connection, connection:
        r._insert(connection, "ledger", {**stored, "title": "Replaced"}, "token-b", seeds)
    assert r._cache.get(r._key("ledger")) is None
    assert r.get_document("ledger")["title"] == "Replaced"
    assert r.token("ledger") == "token-b"
    with r.connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM ledgers WHERE slug=?", ("ledger",)).fetchone()[0]
    assert count == 1


def test_a_reinsert_takes_a_generation_above_the_one_it_replaces_even_when_the_clock_is_behind(tmp_path, monkeypatch):
    r = store(tmp_path)
    monkeypatch.setattr(r.domain, "now_ms", lambda: 50)
    r.create("ledger", CONTENT)
    with r.connect() as connection:
        first = connection.execute("SELECT generation FROM ledgers").fetchone()[0]
    assert first == 50 << 20
    monkeypatch.setattr(r.domain, "now_ms", lambda: 10)
    r.import_document("ledger", document(), replace=True)
    with r.connect() as connection:
        assert connection.execute("SELECT generation FROM ledgers").fetchone()[0] == first + 1
    monkeypatch.setattr(r.domain, "now_ms", lambda: 60)
    r.import_document("ledger", document(), replace=True)
    with r.connect() as connection:
        assert connection.execute("SELECT generation FROM ledgers").fetchone()[0] == 60 << 20


def test_read_only_yields_none_without_a_database_and_a_connection_that_refuses_writes(tmp_path):
    with sqlite.read_only(tmp_path) as connection:
        assert connection is None
    store(tmp_path).create("ledger", CONTENT)
    with sqlite.read_only(tmp_path / "ledgers") as connection:
        assert connection.execute("SELECT slug FROM ledgers").fetchall() == [("ledger",)]
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM ledgers")


def test_import_document_clears_seeds_before_flattening_then_restores_them_on_export(tmp_path, monkeypatch):
    r = store(tmp_path)
    state = document()
    captured = {}
    real_flatten = sqlite.flatten

    def spy(value):
        if "_meta" in value and "seeds" in value.get("_meta", {}):
            captured["seeds_at_flatten_time"] = value["_meta"]["seeds"]
        return real_flatten(value)

    monkeypatch.setattr(sqlite, "flatten", spy)
    r.import_document("ledger", state, token="t" * 24)
    assert captured["seeds_at_flatten_time"] == {}
    assert r.export_document("ledger")["_meta"]["seeds"] == state["_meta"]["seeds"]


def test_create_document_refuses_a_second_write_unless_replacing(tmp_path):
    r = store(tmp_path)
    assert r.create("ledger", CONTENT) is True
    assert r.create("ledger", CONTENT) is False


def _fresh_doc_and_meta(content=CONTENT, size="small"):
    doc = new_ledger.build_doc(content, size)
    ledger_core.validate(doc)
    doc, meta, _ = legacy.fresh(doc)
    return doc, meta


def test_create_document_stores_the_given_token_or_generates_one_with_24_bytes(tmp_path, monkeypatch):
    r = store(tmp_path)
    doc, meta = _fresh_doc_and_meta()
    assert r.create_document("with-token", doc, meta, token="fixed-token-value") is True
    assert r.token("with-token") == "fixed-token-value"

    calls = []
    real_token_urlsafe = sqlite.secrets.token_urlsafe
    monkeypatch.setattr(sqlite.secrets, "token_urlsafe", lambda n: calls.append(n) or real_token_urlsafe(n))
    doc2, meta2 = _fresh_doc_and_meta()
    assert r.create_document("without-token", doc2, meta2) is True
    assert calls == [24]


def test_apply_ops_begins_an_immediate_transaction(tmp_path):
    r = store(tmp_path)
    r.create("ledger", CONTENT)
    statements = []
    r.trace = statements.append
    r.apply_ops("ledger", ops=[{"op": "add", "id": "m1", "thread": "chat", "text": "hi"}])
    assert statements[0] == "BEGIN IMMEDIATE"


def test_registry_round_trips_through_save_registry_and_the_module_level_reader(tmp_path):
    r = store(tmp_path)
    with r.connect() as connection, connection:
        r.save_registry(connection, "bin", {"a": 1, "b": [2, 3]})
    with r.connect() as connection:
        assert r.registry("bin", connection) == {"a": 1, "b": [2, 3]}
    assert read_registry(tmp_path / "ledgers", "bin") == {"a": 1, "b": [2, 3]}
    assert read_registry(tmp_path / "ledgers", "missing-registry") == {}


def test_token_returns_none_before_creation_and_the_stored_value_after(tmp_path):
    r = store(tmp_path)
    assert r.token("ledger") is None
    r.create_document("ledger", *_fresh_doc_and_meta(), token="abc123")
    assert r.token("ledger") == "abc123"


def test_events_since_raises_missing_for_an_unknown_slug(tmp_path):
    r = store(tmp_path)
    with pytest.raises(sqlite.Missing):
        r.events_since("missing", 0)


def test_summaries_lists_newest_first_with_exact_summary_fields(tmp_path):
    r = store(tmp_path)
    r.create("older", CONTENT)
    r.create("newer", {**CONTENT, "title": "Newer"})
    rows = r.summaries()
    assert [row["slug"] for row in rows] == ["newer", "older"]
    assert rows[0]["title"] == "Newer"
    assert set(rows[0]) == set(sqlite.SUMMARY_KEYS) | {"created_at", "finished"}


def test_export_document_begins_a_transaction_and_restores_seeds(tmp_path):
    r = store(tmp_path)
    state = document()
    r.import_document("ledger", state, token="t" * 24)
    statements = []
    r.trace = statements.append
    exported = r.export_document("ledger")
    assert statements[0] == "BEGIN"
    assert exported == state


def test_purge_deletes_every_per_ledger_table_and_drops_the_cache(tmp_path):
    r = store(tmp_path)
    r.create("ledger", CONTENT)
    r.apply_ops("ledger", ops=[{"op": "add", "id": "m1", "thread": "chat", "text": "hi"}])
    with r.connect() as connection, connection:
        r.purge("ledger", connection)
    assert r._cache.get(r._key("ledger")) is None
    with r.connect() as connection:
        for table in sqlite.PER_LEDGER:
            assert connection.execute(f"SELECT 1 FROM {table} WHERE slug=?", ("ledger",)).fetchone() is None


def test_purge_is_safe_to_call_before_the_slug_was_ever_cached(tmp_path):
    r = store(tmp_path)
    r.create("ledger", CONTENT)
    fresh = store(tmp_path)
    with fresh.connect() as connection, connection:
        fresh.purge("ledger", connection)
    assert fresh._cache == {}


def test_delete_forwards_slug_and_now_to_bin_storage(monkeypatch):
    calls = []
    monkeypatch.setattr(bin_storage, "delete", lambda slug, now: calls.append((slug, now)))
    SQLiteLedgerRepository().delete("ledger-x", now=12345)
    assert calls == [("ledger-x", 12345)]


def test_restore_forwards_slug_and_now_and_returns_bin_storages_result(monkeypatch):
    monkeypatch.setattr(bin_storage, "restore", lambda slug, now: (slug, now) == ("ledger-x", 6789))
    r = SQLiteLedgerRepository()
    assert r.restore("ledger-x", now=6789) is True
    assert r.restore("other", now=6789) is False
