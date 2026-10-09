from pathlib import Path

from scripts.swarm_ledger.repository.folder import ledger_folder


def test_the_ledger_folder_is_the_setting_expanded_or_the_shared_home_folder(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert ledger_folder({"LEDGER_DIR": "~/scratch"}) == tmp_path / "scratch"
    assert ledger_folder({"LEDGER_DIR": ""}) == tmp_path / "development-ledger"
    assert ledger_folder({}) == tmp_path / "development-ledger"
    assert isinstance(ledger_folder({}), Path)
