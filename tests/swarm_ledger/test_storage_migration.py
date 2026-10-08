import json
import sys

import pytest

from scripts.swarm_ledger import storage_migration
from scripts.swarm_ledger.repository import repository
from scripts.swarm_ledger.storage_migration import __main__ as command
from tests.swarm_ledger.test_sqlite import document


def run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["agentihooks ledger storage", *argv])
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
    assert repository.export_document("cutover-a") == document()


def test_export_of_an_unknown_ledger_names_it(monkeypatch):
    with pytest.raises(SystemExit, match="unknown-ledger"):
        run(monkeypatch, "export", "unknown-ledger")
