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


def test_a_reader_changing_the_document_cannot_change_the_next_read(repo):
    first = repo.get_document(SLUG)
    rev = first["_meta"]["rev"]
    first["chat"].append({"id": "x", "text": "stray"})
    first["_meta"]["seeds"].clear()
    first["title"] = "changed"
    second = repo.get_document(SLUG)
    assert second["title"] == "Cached"
    assert [m["text"] for m in second["chat"]] == ["first"]
    assert second["_meta"]["rev"] == rev
    assert second["_meta"]["seeds"]


def test_a_write_reply_changed_by_its_caller_cannot_change_the_next_read(repo):
    state, _ = repo.apply_ops(SLUG, ops=[{"op": "add", "thread": "chat", "id": "m3", "text": "third"}])
    rev = state["_meta"]["rev"]
    state["chat"].clear()
    state["_meta"] = {"rev": -1}
    after = repo.get_document(SLUG)
    assert [m["text"] for m in after["chat"]] == ["first", "third"]
    assert after["_meta"]["rev"] == rev


def test_a_read_after_a_refused_write_clears_the_refusal_like_a_sync_does(repo, monkeypatch):
    derive = file_repository.ledger_priorities.derive

    def refuse(doc, ctx):
        ctx.refused.append("comment refused")
        derive(doc, ctx)

    monkeypatch.setattr(file_repository.ledger_priorities, "derive", refuse)
    state, _ = repo.apply_ops(SLUG, ops=[{"op": "add", "thread": "chat", "id": "m5", "text": "fifth"}])
    monkeypatch.setattr(file_repository.ledger_priorities, "derive", derive)
    assert "comment refused" in state["_meta"]["warnings"]
    assert "comment refused" not in repo.get_document(SLUG)["_meta"]["warnings"]
    assert "comment refused" not in repo.get_document(SLUG, reconcile=False)["_meta"]["warnings"]


def test_a_page_edit_landing_while_a_sync_runs_is_folded_in_on_the_next_read(repo, monkeypatch):
    html_path = core.paths(SLUG)[0]
    derive = file_repository.ledger_priorities.derive

    def edit_mid_sync(doc, ctx):
        html = html_path.read_text(encoding="utf-8")
        html_path.write_text(html.replace('"title": "Cached"', '"title": "Racing"', 1), encoding="utf-8")
        derive(doc, ctx)

    monkeypatch.setattr(file_repository.ledger_priorities, "derive", edit_mid_sync)
    file_repository.SYNCED.clear()
    repo.get_document(SLUG)
    monkeypatch.setattr(file_repository.ledger_priorities, "derive", derive)
    assert repo.get_document(SLUG)["title"] == "Racing"


def test_a_page_edit_landing_right_after_the_seed_rewrite_is_folded_in_on_the_next_read(repo, monkeypatch):
    html_path = core.paths(SLUG)[0]
    atomic_write = core.atomic_write

    def edit_after_rewrite(path, text):
        written = atomic_write(path, text)
        if path == html_path:
            html_path.write_text(text.replace('"title": "Cached"', '"title": "Racer"', 1), encoding="utf-8")
        return written

    monkeypatch.setattr(core, "atomic_write", edit_after_rewrite)
    repo.apply_ops(SLUG, ops=[{"op": "add", "thread": "chat", "id": "m4", "text": "fourth"}])
    monkeypatch.setattr(core, "atomic_write", atomic_write)
    assert repo.get_document(SLUG)["title"] == "Racer"


def test_a_read_without_reconcile_does_not_fold_a_page_edit(repo, loads):
    html_path = core.paths(SLUG)[0]
    html = html_path.read_text(encoding="utf-8")
    html_path.write_text(html.replace('"title": "Cached"', '"title": "Edited"', 1), encoding="utf-8")
    assert repo.get_document(SLUG, reconcile=False)["title"] == "Cached"
    assert loads == []


def test_a_sync_that_rewrites_nothing_still_serves_the_next_read(repo, loads):
    file_repository.SYNCED.clear()
    repo.get_document(SLUG)
    repo.get_document(SLUG)
    assert len(loads) == 1


def test_a_removed_stored_document_is_not_served_from_the_last_sync(repo):
    core.paths(SLUG)[1].unlink()
    with pytest.raises(ValueError):
        repo.get_document(SLUG, reconcile=False)


def test_a_read_syncs_again_once_an_hour_so_time_based_sweeps_still_run(repo, loads, monkeypatch):
    now = core.now_ms()
    monkeypatch.setattr(core, "now_ms", lambda: now + file_repository.SWEEP_MS)
    repo.get_document(SLUG)
    assert len(loads) == 1


class LaterClock:
    def __getattr__(self, name):
        return getattr(core, name)

    def now_ms(self):
        return core.now_ms() + file_repository.SWEEP_MS


def test_the_hourly_sync_follows_the_repository_clock(repo, loads):
    FileLedgerRepository(LaterClock()).get_document(SLUG)
    assert len(loads) == 1
