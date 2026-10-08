import json

import pytest

from scripts.swarm_ledger import ledger_core, new_ledger
from scripts.swarm_ledger.repository import legacy, sqlite
from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository
from tests.swarm_ledger.test_sqlite import document

TOKEN = "legacy-token-0123456789abcdef"
PAGE = (
    '<meta name="ledger-token" content="{token}">\n<script id="ledger-data" type="application/json">{seed}</script>\n'
)


def folder(tmp_path):
    directory = tmp_path / "ledgers"
    directory.mkdir(exist_ok=True)
    return directory


def page(seed, token=TOKEN):
    return PAGE.format(token=token, seed=json.dumps(seed))


def stored(directory):
    return SQLiteLedgerRepository(directory / sqlite.DATABASE)


def test_a_json_ledger_is_backed_up_imported_losslessly_and_set_aside(tmp_path):
    directory = folder(tmp_path)
    state = document()
    source = json.dumps(state, indent=2)
    (directory / "old.json").write_text(source, encoding="utf-8")
    (directory / "old.html").write_text(page({"title": "x"}), encoding="utf-8")
    repo = stored(directory)
    assert repo.exists("old")
    assert repo.export_document("old") == ledger_core.normalize(state)
    assert repo.token("old") == TOKEN
    assert not (directory / "old.json").exists() and not (directory / "old.html").exists()
    (backup,) = (directory / legacy.BACKUP).iterdir()
    assert backup.name.startswith("old-")
    assert (backup / "old.json").read_text(encoding="utf-8") == source
    assert (backup / "old.html").read_text(encoding="utf-8") == page({"title": "x"})


def test_a_json_ledger_from_before_a_field_existed_imports_in_the_shape_the_server_reads(tmp_path):
    directory = folder(tmp_path)
    state = {
        "title": "Old",
        "overview": "o",
        "phases": [],
        "questions": [],
        "followups": [],
        "_meta": {"rev": 3, "stamps": {}, "events": [], "seeds": {}},
    }
    (directory / "old.json").write_text(json.dumps(state), encoding="utf-8")
    repo = stored(directory)
    written, rejected = repo.apply_ops("old", ops=[{"op": "add", "id": "m1", "thread": "chat", "text": "hello"}])
    assert rejected == []
    assert written["artifact_trash"] == [] and [m["id"] for m in written["chat"]] == ["m1"]
    (backup,) = (directory / legacy.BACKUP).iterdir()
    assert json.loads((backup / "old.json").read_text(encoding="utf-8")) == state


def test_a_page_without_json_becomes_a_fresh_ledger_with_its_token(tmp_path):
    directory = folder(tmp_path)
    seed = new_ledger.build_doc({"title": "Fresh", "overview": "o", "phases": [{"title": "One", "description": "d"}]})
    (directory / "fresh.html").write_text(page(seed), encoding="utf-8")
    repo = stored(directory)
    state = repo.get_document("fresh")
    assert state["title"] == "Fresh" and state["_meta"]["rev"] == 1
    assert "seeds" not in state["_meta"] and "notifications" in state
    assert repo.token("fresh") == TOKEN
    assert not (directory / "fresh.html").exists()


def test_a_dropped_file_replaces_the_stored_ledger_once(tmp_path):
    directory = folder(tmp_path)
    repo = stored(directory)
    repo.import_document("same", document())
    replaced = {**document(), "title": "Replaced"}
    (directory / "same.json").write_text(json.dumps(replaced), encoding="utf-8")
    assert repo.get_document("same")["title"] == "Replaced"
    assert not (directory / "same.json").exists()
    assert repo.export_document("same") == ledger_core.normalize(replaced)


def test_an_unreadable_file_is_kept_and_reported_on_a_sweep_but_refused_by_name(tmp_path, capsys):
    directory = folder(tmp_path)
    (directory / "broken.json").write_text("{not json", encoding="utf-8")
    repo = stored(directory)
    assert repo.list_summaries() == []
    assert "legacy ledger broken not imported" in capsys.readouterr().err
    assert (directory / "broken.json").exists()
    with pytest.raises(ValueError):
        repo.get_document("broken")


def test_bin_indexes_merge_into_the_registry_and_are_set_aside(tmp_path):
    directory = folder(tmp_path)
    (directory / ".bin.json").write_text(json.dumps({"gone": 5}), encoding="utf-8")
    (directory / ".bin-restored.json").write_text("not json", encoding="utf-8")
    repo = stored(directory)
    repo.list_summaries()
    assert repo.registry("bin") == {"gone": 5}
    assert repo.registry("restored") == {}
    assert not (directory / ".bin.json").exists() and not (directory / ".bin-restored.json").exists()
    kept = sorted(path.name for backup in (directory / legacy.BACKUP).iterdir() for path in backup.iterdir())
    assert kept == [".bin-restored.json", ".bin.json"]


def test_a_failed_backup_leaves_the_file_in_place(tmp_path, monkeypatch):
    directory = folder(tmp_path)
    (directory / "kept.json").write_text(json.dumps(document()), encoding="utf-8")
    monkeypatch.setattr(legacy.shutil, "copy2", lambda source, target: (target.write_text("torn"), target)[1])
    with pytest.raises(OSError, match="does not match"):
        stored(directory).exists("kept")
    assert (directory / "kept.json").exists()
