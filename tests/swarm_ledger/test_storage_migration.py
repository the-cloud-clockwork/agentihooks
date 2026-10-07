import copy
import json

import pytest

from scripts.swarm_ledger.repository import FileLedgerRepository
from scripts.swarm_ledger.repository.file import core
from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository
from scripts.swarm_ledger.storage_migration import import_directory
from tests.swarm_ledger.test_sqlite import document


def test_directory_import_resumes_and_preserves_bin_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    for slug in ("one", "two"):
        state = document()
        (tmp_path / f"{slug}.json").write_text(json.dumps(state), encoding="utf-8")
        (tmp_path / f"{slug}.html").write_text("page", encoding="utf-8")
    (tmp_path / ".bin.json").write_text('{"two":12,"extension":{"unknown":true}}', encoding="utf-8")
    (tmp_path / ".bin-restored.json").write_text('{"one":9}', encoding="utf-8")
    db = tmp_path / "ledger-shadow.sqlite3"
    original = SQLiteLedgerRepository.import_document
    with monkeypatch.context() as patch:

        def interrupted(self, slug, *args, **kwargs):
            if slug == "two":
                raise RuntimeError("interrupted")
            return original(self, slug, *args, **kwargs)

        patch.setattr(SQLiteLedgerRepository, "import_document", interrupted)
        with pytest.raises(RuntimeError, match="interrupted"):
            import_directory(tmp_path, db)
    assert SQLiteLedgerRepository(db).get_document("one") == FileLedgerRepository().get_document("one", reconcile=False)
    assert import_directory(tmp_path, db) == ["one", "two"]
    assert import_directory(tmp_path, db) == ["one", "two"]
    repo = SQLiteLedgerRepository(db)
    assert repo.get_document("two") == FileLedgerRepository().get_document("two", reconcile=False)
    assert repo.lifecycle("two")["deleted_at"] == 12
    assert repo.lifecycle("one")["restored_at"] == 9
    assert repo.registry("bin") == {"two": 12, "extension": {"unknown": True}}
    assert repo.registry("restored") == {"one": 9}


def test_seed_replay_tracks_reorder_delete_and_unknown_fields(tmp_path):
    repo = SQLiteLedgerRepository(tmp_path / "shadow.sqlite3")
    state = document()
    state["tasks"].reverse()
    state["tasks"][0].pop("unknown")
    state["tasks"][0]["comments"][0]["text"] = "updated"
    state["_meta"]["seeds"]["2"] = copy.deepcopy({k: v for k, v in state.items() if k != "_meta"})
    state["_meta"]["rev"] = 2
    repo.import_document("ledger", state)
    assert repo.get_document("ledger") == state
    assert repo.get_seed("ledger", "2") == state["_meta"]["seeds"]["2"]


def test_migration_command_imports_the_requested_directory(tmp_path, monkeypatch, capsys):
    from scripts.swarm_ledger.storage_migration.__main__ import main

    monkeypatch.setattr(
        "sys.argv", ["storage_migration", "--directory", str(tmp_path), "--database", str(tmp_path / "custom.sqlite3")]
    )
    main()
    assert json.loads(capsys.readouterr().out) == {"verified": []}
    assert (tmp_path / "custom.sqlite3").exists()


def test_html_only_seed_is_imported_without_creating_a_file_snapshot(tmp_path, monkeypatch):
    assert import_directory.__module__ != "scripts.swarm_ledger.storage_migration.__init__"
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(core, "now_ms", lambda: 123)
    files = FileLedgerRepository()
    files.create(
        "legacy", {"title": "Legacy", "overview": "o", "sources": [], "phases": [{"title": "One", "description": "d"}]}
    )
    snapshot = tmp_path / "legacy.json"
    snapshot.unlink()
    seed = core.parse_seed((tmp_path / "legacy.html").read_text(encoding="utf-8"))
    from scripts.swarm_ledger.repository.file import load_state

    document, meta, _ = load_state(snapshot, seed, core)
    meta["updated_at"] = (tmp_path / "legacy.html").stat().st_mtime_ns // 1_000_000
    database = tmp_path / "import.sqlite3"
    assert import_directory(tmp_path, database) == ["legacy"]
    assert SQLiteLedgerRepository(database).get_document("legacy") == {**document, "_meta": meta}
    assert not snapshot.exists()
    monkeypatch.setattr(core, "now_ms", lambda: 456)
    assert import_directory(tmp_path, database) == ["legacy"]
    assert SQLiteLedgerRepository(database).get_document("legacy") == {**document, "_meta": meta}


def test_migration_command_defaults_and_help(tmp_path, monkeypatch, capsys):
    from scripts.swarm_ledger.storage_migration.__main__ import main

    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr("sys.argv", ["storage_migration"])
    main()
    assert json.loads(capsys.readouterr().out) == {"verified": []}
    assert (tmp_path / "ledger-shadow.sqlite3").exists()
    monkeypatch.setattr("sys.argv", ["storage_migration", "--help"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 0
    assert "Import and verify authoritative ledgers in SQLite shadow storage" in capsys.readouterr().out
