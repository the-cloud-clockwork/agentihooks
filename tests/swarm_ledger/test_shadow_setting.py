import pytest

from scripts.swarm_ledger.repository import FileLedgerRepository, bin_storage
from scripts.swarm_ledger.repository.file import core

CONTENT = {"title": "Setting", "overview": "o", "sources": [], "phases": [{"title": "One", "description": "d"}]}


@pytest.mark.parametrize("value", [None, "0", ""])
def test_writes_and_lifecycle_skip_the_shadow_unless_enabled(tmp_path, monkeypatch, value):
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    if value is None:
        monkeypatch.delenv("LEDGER_SQLITE_SHADOW", raising=False)
    else:
        monkeypatch.setenv("LEDGER_SQLITE_SHADOW", value)
    files = FileLedgerRepository()
    files.create("setting", CONTENT)
    state, rejected = files.apply_ops("setting", ops=[{"op": "join", "id": "j", "by": "eng"}])
    assert rejected == []
    assert "eng" in state["_meta"]["members"]
    files.delete("setting", now=12)
    assert files.restore("setting", now=15)
    assert bin_storage.purge_expired(now=15) == []
    assert not (tmp_path / "ledger-shadow.sqlite3").exists()


def test_enabled_setting_writes_the_shadow(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    monkeypatch.setenv("LEDGER_SQLITE_SHADOW", "1")
    FileLedgerRepository().create("setting", CONTENT)
    assert (tmp_path / "ledger-shadow.sqlite3").exists()
