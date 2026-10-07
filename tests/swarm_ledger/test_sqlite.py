import copy
import json

import pytest

from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository


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


def test_import_is_lossless_idempotent_and_restarts(tmp_path):
    path = tmp_path / "shadow.sqlite3"
    repo = SQLiteLedgerRepository(path)
    state = document()
    repo.import_document("ledger", state, deleted_at=12, restored_at=9)
    assert repo.get_document("ledger") == state
    assert repo.events_since("ledger", 0) == state["_meta"]["events"]
    assert repo.events_since("ledger", 1) == []
    assert repo.lifecycle("ledger") == {"deleted_at": 12, "restored_at": 9}
    with repo.connect() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    statements = []
    repo.trace = statements.append
    repo.import_document("ledger", state, deleted_at=12, restored_at=9)
    assert not [s for s in statements if s.startswith(("INSERT", "UPDATE", "DELETE"))]
    assert SQLiteLedgerRepository(path).get_document("ledger") == state
    assert repo.get_seed("ledger", "0") == state["_meta"]["seeds"]["0"]
    assert repo.get_seed("ledger", "1") == state["_meta"]["seeds"]["1"]
    with pytest.raises(KeyError, match="missing"):
        repo.get_document("missing")


def test_changed_item_writes_only_affected_rows_and_seed_delta(tmp_path):
    repo = SQLiteLedgerRepository(tmp_path / "shadow.sqlite3")
    state = document()
    repo.import_document("ledger", state)
    changed = copy.deepcopy(state)
    changed["tasks"][1]["proof"] = {"output": "new"}
    changed["_meta"]["rev"] = 2
    changed["_meta"]["events"].append({"rev": 2, "id": "next"})
    changed["_meta"]["seeds"]["2"] = {k: v for k, v in changed.items() if k != "_meta"}
    statements = []
    repo.trace = statements.append
    repo.import_document("ledger", changed)
    writes = [s for s in statements if s.startswith(("INSERT", "UPDATE", "DELETE"))]
    assert len(writes) < 15
    assert not any("second" in s for s in writes)
    assert not any(json.dumps(changed) in s for s in writes)
    assert repo.get_document("ledger") == changed
    changed["_meta"]["seeds"].pop("0")
    repo.import_document("ledger", changed)
    assert repo.get_document("ledger") == changed
    assert repo.get_seed("ledger", "1") == state["_meta"]["seeds"]["1"]
    with pytest.raises(KeyError):
        repo.get_seed("ledger", "0")


def test_failed_verification_rolls_back_every_row(tmp_path, monkeypatch):
    repo = SQLiteLedgerRepository(tmp_path / "shadow.sqlite3")
    before = document()
    repo.import_document("ledger", before, registries={"bin": {}, "restored": {}})
    after = copy.deepcopy(before)
    after["_meta"]["rev"] = 2
    after["title"] = "Changed"
    after["notifications"] = [{"id": "n"}]
    after["priorities"] = [{"id": "p"}]
    after["_meta"]["events"].append({"rev": 2, "id": "change"})
    with monkeypatch.context() as patch:
        patch.setattr(repo, "verify", lambda *args: (_ for _ in ()).throw(RuntimeError("crash")))
        with pytest.raises(RuntimeError, match="crash"):
            repo.import_document("ledger", after, registries={"bin": {"ledger": 12}, "restored": {}})
    assert SQLiteLedgerRepository(repo.path).get_document("ledger") == before
    assert repo.events_since("ledger", 0) == before["_meta"]["events"]
    assert repo.registry("bin") == {}


@pytest.mark.parametrize(
    "method, args",
    [
        ("apply_ops", ("ledger",)),
        ("list_summaries", ()),
        ("create", ("ledger", {})),
        ("delete", ("ledger",)),
        ("restore", ("ledger",)),
    ],
)
def test_standalone_shadow_refuses_mutations_without_file_authority(tmp_path, method, args):
    repo = SQLiteLedgerRepository(tmp_path / "shadow.sqlite3")
    with pytest.raises(RuntimeError) as error:
        getattr(repo, method)(*args)
    assert str(error.value) == "SQLite is a shadow; mutations require the authoritative file repository"


def test_legacy_metadata_without_events_is_preserved(tmp_path):
    state = document()
    state["_meta"].pop("events")
    repo = SQLiteLedgerRepository(tmp_path / "shadow.sqlite3")
    repo.import_document("legacy", state)
    assert repo.get_document("legacy") == state
    assert repo.events_since("legacy", 0) == []
    with pytest.raises(KeyError) as error:
        repo.events_since("missing", 0)
    assert error.value.args == ("missing",)
    with pytest.raises(KeyError) as error:
        repo.lifecycle("missing")
    assert error.value.args == ("missing",)


def test_nested_database_and_verification_failures(tmp_path, monkeypatch):
    repo = SQLiteLedgerRepository(tmp_path / "nested" / "storage" / "shadow.sqlite3")
    state = document()
    with monkeypatch.context() as patch:
        patch.setattr(repo, "_document", lambda *args: {})
        with pytest.raises(ValueError) as error:
            repo.import_document("ledger", state)
        assert str(error.value) == "SQLite shadow document differs for ledger"
    with pytest.raises(KeyError):
        repo.get_document("ledger")
    import scripts.swarm_ledger.repository.sqlite as storage

    with monkeypatch.context() as patch:
        patch.setattr(storage, "sync_values", lambda *args: None)
        with pytest.raises(ValueError) as error:
            repo.import_document("ledger", state, registries={"bin": {"ledger": 12}})
        assert str(error.value) == "SQLite shadow registry differs for bin"
    assert repo.registry("bin") == {}


def test_registry_names_and_unknown_values_survive_import(tmp_path):
    repo = SQLiteLedgerRepository(tmp_path / "shadow.sqlite3")
    repo.import_registry("bin", {"ledger": 12, "unknown": {"text": "é"}})
    assert repo.registry("bin") == {"ledger": 12, "unknown": {"text": "é"}}
    assert repo.registry("missing") == {}


def test_json_only_authority_is_retained_during_lifecycle_reconciliation(tmp_path):
    state = document()
    repo = SQLiteLedgerRepository(tmp_path / "shadow.sqlite3")
    repo.import_document("orphan", state)
    (tmp_path / "orphan.json").write_text(json.dumps(state), encoding="utf-8")
    repo.apply_lifecycle(tmp_path, {"bin": {}, "restored": {}})
    assert repo.get_document("orphan") == state


def test_event_presence_metadata_keeps_the_normalized_array_shape(tmp_path):
    repo = SQLiteLedgerRepository(tmp_path / "shadow.sqlite3")
    repo.import_document("metadata", document())
    with repo.connect() as connection:
        assert connection.execute(
            "SELECT kind,value FROM fields WHERE slug=? AND path=?", ("metadata", '["_meta","events"]')
        ).fetchone() == ("array", "null")
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM fields WHERE slug=? AND parent=?", ("metadata", '["_meta","events"]')
            ).fetchone()[0]
            == 0
        )


def test_disabled_sql_trace_produces_no_callback_errors(tmp_path, monkeypatch):
    import sqlite3
    import sys

    errors = []
    monkeypatch.setattr(sys, "unraisablehook", errors.append)
    sqlite3.enable_callback_tracebacks(True)
    try:
        repo = SQLiteLedgerRepository(tmp_path / "shadow.sqlite3")
        repo.import_document("trace", document())
    finally:
        sqlite3.enable_callback_tracebacks(False)
    assert errors == []


def test_initial_lifecycle_import_reads_unicode_in_ascii_locale(tmp_path):
    import locale
    import sys

    if sys.flags.utf8_mode:
        pytest.skip("UTF8 mode overrides the locale encoding")
    state = document()
    (tmp_path / "unicode.html").write_text("page", encoding="utf-8")
    (tmp_path / "unicode.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    repo = SQLiteLedgerRepository(tmp_path / "shadow.sqlite3")
    before = locale.setlocale(locale.LC_CTYPE)
    locale.setlocale(locale.LC_CTYPE, "C")
    try:
        repo.apply_lifecycle(tmp_path, {"bin": {"unicode": 12}, "restored": {}})
        assert repo.get_document("unicode") == state
        assert repo.lifecycle("unicode") == {"deleted_at": 12, "restored_at": None}
    finally:
        locale.setlocale(locale.LC_CTYPE, before)
