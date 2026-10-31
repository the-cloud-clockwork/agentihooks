import json
import sys
from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import ledger
from scripts.swarm_ledger.api import routes
from scripts.swarm_ledger.repository import sqlite as store
from tests.swarm_ledger.api_fakes import as_services

SLUG = "freezes-and-raw-tasks"
STATE = {
    "phases": [{"id": "p1", "title": "Build"}],
    "tasks": [
        {"id": "t1", "title": "Hive placement", "lane": "eng", "state": "open", "phase": "p1"},
        {"id": "t2", "title": "Hive docs", "lane": "eng", "state": "done", "phase": "p1"},
        {"id": "t3", "title": "Hive chart", "lane": "eng", "state": "open", "out_of_scope": True},
    ],
    "freezes": [{"id": "f1", "verb": "freeze", "target": "phases/p1", "by": "operator", "at": 0, "reason": "paused"}],
    "_meta": {"rev": 1},
}


@pytest.fixture
def reads(tmp_path, monkeypatch):
    repo = store.SQLiteLedgerRepository(tmp_path / store.DATABASE)
    repo.import_document(SLUG, STATE)
    services = as_services(SimpleNamespace(repository=repo))
    seen = []

    def resource(slug, path, collection=False):
        seen.append((slug, path, collection))
        return routes.ledger_read(services, slug, path, {})["data"]

    def whole(*_args, **_kwargs):
        raise AssertionError("a freezes or raw tasks read never loads the whole ledger")

    monkeypatch.setattr(ledger, "resource", resource)
    monkeypatch.setattr(ledger, "call", whole)
    return seen


def test_tasks_json_prints_the_raw_collection_with_done_and_out_of_scope(reads, capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["ledger", "--slug", SLUG, "tasks", "--json"])
    ledger.main()
    rows = json.loads(capsys.readouterr().out)
    assert [row["id"] for row in rows] == ["t1", "t2", "t3"]
    assert reads == [(SLUG, "tasks", True)]


def test_freezes_prints_the_freeze_and_focus_records(reads, capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["ledger", "--slug", SLUG, "freezes"])
    ledger.main()
    rows = json.loads(capsys.readouterr().out)
    assert rows == STATE["freezes"]
    assert reads == [(SLUG, "freezes", True)]


def test_tasks_without_json_keeps_the_filtered_plain_text_form(reads, capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["ledger", "--slug", SLUG, "tasks"])
    ledger.main()
    assert capsys.readouterr().out.splitlines() == ["t1  open  eng  Hive placement"]


def test_tasks_and_freezes_document_their_forms(capsys):
    parser = ledger.build_parser()
    for argv in (["tasks", "--help"], ["freezes", "--help"]):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)
    out = " ".join(capsys.readouterr().out.split())
    assert "json print the raw task collection as JSON" in out
    assert "freezes print the freeze and focus records as JSON" in out
