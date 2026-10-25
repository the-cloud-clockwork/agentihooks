import pytest

from scripts.swarm_ledger.repository import sqlite
from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository


def store(tmp_path):
    return SQLiteLedgerRepository(tmp_path / "ledgers" / sqlite.DATABASE)


def content(title="Café"):
    return {"title": title, "overview": "Résumé", "sources": [], "phases": [{"title": "one", "description": "d"}]}


def test_creator_refuses_invalid_content(tmp_path):
    with pytest.raises(SystemExit) as refused:
        store(tmp_path).create("rejected", {})
    assert str(refused.value) == "content rejected:\n  title is empty\n  no phases"


def test_repository_reads_keep_utf8_when_the_locale_is_ascii(tmp_path):
    import locale
    import sys

    if sys.flags.utf8_mode:
        pytest.skip("UTF8 mode overrides the locale encoding")
    repo = store(tmp_path)
    repo.create("utf8", content())
    repo.apply_ops("utf8", changes=[{"path": "phases/p1/done", "value": True}])
    before = locale.setlocale(locale.LC_CTYPE)
    locale.setlocale(locale.LC_CTYPE, "C")
    try:
        assert repo.get_document("utf8")["title"] == "Café"
        assert any(row["title"] == "Café" for row in repo.list_summaries())
    finally:
        locale.setlocale(locale.LC_CTYPE, before)
