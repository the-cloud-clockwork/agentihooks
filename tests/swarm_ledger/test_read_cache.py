import json

import pytest

from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger import new_ledger
from scripts.swarm_ledger.repository import FileLedgerRepository
from scripts.swarm_ledger.repository import file as file_repository

SLUG = "read-cache-2026-01-01"


@pytest.fixture(autouse=True)
def cache_ledger_dir(ledger_dir, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", ledger_dir)


@pytest.fixture
def repo():
    doc = new_ledger.build_doc(
        {"title": "Cached", "overview": "o", "sources": [], "phases": [{"title": "one", "description": "d"}]}
    )
    html_path, json_path = core.paths(SLUG)
    html_path.write_text(new_ledger.render(doc, SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    core.sync(SLUG, ops=[{"op": "add", "thread": "chat", "id": "m1", "text": "first"}])
    return FileLedgerRepository(core)


@pytest.fixture
def loads(monkeypatch):
    calls = []
    real = file_repository.load_state

    def counted(*args, **kwargs):
        calls.append(args[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(file_repository, "load_state", counted)
    return calls


def test_a_read_with_nothing_changed_on_disk_skips_the_sync(repo, loads):
    for _ in range(5):
        assert repo.get_document(SLUG)["chat"][-1]["text"] == "first"
        assert repo.get_document(SLUG, reconcile=False)["chat"][-1]["text"] == "first"
    assert loads == []


def test_a_write_still_loads_the_stored_document(repo, loads):
    state, _ = repo.apply_ops(SLUG, ops=[{"op": "add", "thread": "chat", "id": "m2", "text": "second"}])
    assert len(loads) == 1
    assert [m["text"] for m in repo.get_document(SLUG)["chat"]] == ["first", "second"]
    assert len(loads) == 1


def test_an_outside_edit_of_the_page_seed_is_folded_in_on_the_next_read(repo):
    html_path = core.paths(SLUG)[0]
    html = html_path.read_text(encoding="utf-8")
    html_path.write_text(html.replace('"title": "Cached"', '"title": "Edited"', 1), encoding="utf-8")
    assert repo.get_document(SLUG)["title"] == "Edited"


def test_an_outside_edit_of_the_stored_document_is_read(repo):
    json_path = core.paths(SLUG)[1]
    stored = json.loads(json_path.read_text(encoding="utf-8"))
    stored["overview"] = "rewritten"
    json_path.write_text(json.dumps(stored), encoding="utf-8")
    assert repo.get_document(SLUG, reconcile=False)["overview"] == "rewritten"


def test_a_reader_reassigning_fields_cannot_change_the_next_read(repo):
    first = repo.get_document(SLUG)
    rev = first["_meta"]["rev"]
    first["title"] = "changed"
    first["_meta"] = {}
    second = repo.get_document(SLUG)
    second["_meta"]["seeds"] = {}
    third = repo.get_document(SLUG)
    assert third["title"] == "Cached"
    assert third["_meta"]["rev"] == rev
    assert third["_meta"]["seeds"]


def test_a_write_reply_reassigning_meta_cannot_change_the_next_read(repo):
    state, _ = repo.apply_ops(SLUG, ops=[{"op": "add", "thread": "chat", "id": "m3", "text": "third"}])
    state["_meta"] = {"rev": -1}
    assert repo.get_document(SLUG)["_meta"]["rev"] > 0
