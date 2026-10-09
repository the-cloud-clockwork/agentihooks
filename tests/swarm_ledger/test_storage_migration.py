import json
import sys

import pytest

from scripts.swarm_ledger import ledger_core, storage_migration
from scripts.swarm_ledger.repository import repository
from scripts.swarm_ledger.storage_migration import __main__ as command
from tests.swarm_ledger.test_sqlite import document


def run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["storage-entry", *argv])
    command.main()


def test_export_then_import_round_trips_the_complete_document(tmp_path, monkeypatch, capsys):
    repository.import_document("interchange-a", document(), replace=True)
    out = tmp_path / "exported.json"
    run(monkeypatch, "export", "interchange-a", "--out", str(out))
    assert json.loads(capsys.readouterr().out) == {"exported": "interchange-a", "out": str(out)}
    assert json.loads(out.read_text(encoding="utf-8")) == document()
    run(monkeypatch, "import", str(out), "--slug", "interchange-b")
    assert json.loads(capsys.readouterr().out) == {"imported": "interchange-b"}
    assert repository.export_document("interchange-b") == document()
    run(monkeypatch, "export", "interchange-b")
    assert json.loads(capsys.readouterr().out) == document()


def test_import_refuses_an_existing_slug_unless_replacing(tmp_path, monkeypatch, capsys):
    repository.import_document("interchange-c", document(), replace=True)
    source = tmp_path / "interchange-c.json"
    source.write_text(json.dumps({**document(), "title": "New"}), encoding="utf-8")
    with pytest.raises(SystemExit, match="exists"):
        run(monkeypatch, "import", str(source))
    run(monkeypatch, "import", str(source), "--replace")
    assert repository.export_document("interchange-c")["title"] == "New"


def test_import_refuses_a_file_that_is_not_an_exported_ledger(tmp_path, monkeypatch):
    source = tmp_path / "plain.json"
    source.write_text(json.dumps({"title": "no meta"}), encoding="utf-8")
    with pytest.raises(SystemExit, match="not an exported ledger"):
        run(monkeypatch, "import", str(source))
    with pytest.raises(ValueError, match="invalid slug"):
        storage_migration.load(source, "Bad Slug")


def test_cutover_imports_every_file_left_in_the_ledger_folder(monkeypatch, capsys, ledger_dir):
    (ledger_dir / "cutover-a.json").write_text(json.dumps(document()), encoding="utf-8")
    run(monkeypatch, "cutover")
    assert "cutover-a" in json.loads(capsys.readouterr().out)["stored"]
    assert not (ledger_dir / "cutover-a.json").exists()
    assert repository.export_document("cutover-a") == ledger_core.normalize(document())


def test_export_of_an_unknown_ledger_names_it(monkeypatch):
    with pytest.raises(SystemExit, match="unknown-ledger"):
        run(monkeypatch, "export", "unknown-ledger")


def test_load_keeps_an_existing_ledger_unless_told_to_replace(tmp_path):
    repository.import_document("interchange-d", document(), replace=True)
    source = tmp_path / "interchange-d.json"
    source.write_text(json.dumps({**document(), "title": "Café"}), encoding="utf-8")
    with pytest.raises(ValueError, match="exists"):
        storage_migration.load(source)
    assert storage_migration.load(source, replace=True) == "interchange-d"
    assert repository.export_document("interchange-d")["title"] == "Café"


def test_the_command_names_itself_and_each_action_and_needs_one(monkeypatch, capsys):
    with pytest.raises(SystemExit) as helped:
        run(monkeypatch, "--help")
    shown = " ".join(capsys.readouterr().out.split())
    assert helped.value.code == 0
    assert shown.startswith("usage: agentihooks ledger storage [-h] {export,import,cutover}")
    assert " ".join(command.__doc__.split()) in shown
    assert (
        "{export,import,cutover} export print or write the complete stored document import store an exported "
        "document cutover import every ledger file left in the ledger folder options:"
    ) in shown
    with pytest.raises(SystemExit) as bare:
        run(monkeypatch)
    assert bare.value.code == 2
