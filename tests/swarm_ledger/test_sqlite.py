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
            "seeds": {"0": {**seed, "title": "Old"}, "1": copy.deepcopy(seed)},
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
