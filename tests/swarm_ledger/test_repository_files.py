import json
import os

import ledger_core as core
import pytest
from scripts.swarm_ledger.repository.file import FileLedgerRepository

from scripts.swarm_ledger.repository import bin_storage


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    return FileLedgerRepository()


def content(title="Café"):
    return {"title": title, "overview": "Résumé", "sources": [], "phases": [{"title": "one", "description": "d"}]}


def test_repository_creates_the_same_utf8_document_and_records_events(files):
    assert files.create("unicode", content(), "swarm")
    doc = files.get_document("unicode")
    assert doc["title"] == "Café" and doc["size"] == "swarm"
    assert core.paths("unicode")[1].read_bytes().endswith(b"\n")
    assert b"Caf\xc3\xa9" in core.paths("unicode")[1].read_bytes()
    before = doc["_meta"]["rev"]
    state, rejected = files.apply_ops("unicode", ops=[{"op": "add", "thread": "chat", "id": "one", "text": "héllo"}])
    assert rejected == [] and state["chat"][0]["text"] == "héllo"
    assert len(files.events_since("unicode", before)) == 1
    assert files.events_since("unicode", state["_meta"]["rev"]) == []


def test_legacy_page_without_json_seeds_a_document(files):
    files.create("legacy", content())
    core.paths("legacy")[1].unlink()
    doc = files.get_document("legacy")
    assert doc["title"] == "Café" and doc["size"] == "small" and doc["_meta"]["events"] == []
    assert "seeds" in doc["_meta"] and "members" in doc["_meta"]


@pytest.mark.parametrize("state", [{}, {"_meta": []}, {"_meta": {}}, []])
def test_invalid_document_metadata_keeps_the_legacy_refusal(files, state):
    files.create("invalid", content())
    core.paths("invalid")[1].write_text(json.dumps(state))
    with pytest.raises(ValueError, match="has no ledger _meta"):
        files.get_document("invalid")


def test_broken_seed_keeps_json_authority_and_reports_the_error(files):
    files.create("broken", content())
    files.write_page("broken", "broken page")
    doc = files.get_document("broken")
    assert doc["title"] == "Café"
    assert doc["_meta"]["seed_error"].startswith("HTML seed unreadable, agent edits ignored until fixed:")
    assert files.read_page("broken") == "broken page"


def test_missing_state_and_unreadable_seed_are_refused(files):
    files.write_page("missing", "broken page")
    with pytest.raises(ValueError, match="missing and the HTML seed is unreadable"):
        files.get_document("missing")


def test_summary_fallbacks_skip_bad_documents_and_keep_file_order(files):
    files.create("first", content("First"))
    files.create("second", content("Second"))
    doc = files.read_snapshot("second")
    doc["title"] = ""
    doc["overview"] = ""
    doc["phases"][0]["out_of_scope"] = True
    core.paths("second")[1].write_text(json.dumps(doc))
    files.write_page("second", "broken page")
    files.write_page("bad", "broken page")
    files.write_page("bad-without-json", "broken page")
    core.paths("bad")[1].write_text("bad json")
    os.utime(core.paths("first")[0], (2, 2))
    os.utime(core.paths("second")[0], (1, 1))
    rows = files.list_summaries()
    assert [row["slug"] for row in rows] == ["first", "second"]
    assert (rows[1]["title"], rows[1]["overview"], rows[1]["open"], rows[1]["done"]) == ("second", "", 0, 0)


def test_creator_keeps_content_and_orphan_refusals(files):
    with pytest.raises(SystemExit) as refused:
        files.create("rejected", {})
    assert str(refused.value) == "content rejected:\n  title is empty\n  no phases"
    core.paths("orphan")[1].write_text("{}")
    with pytest.raises(SystemExit, match="exists without its HTML"):
        files.create("orphan", content())


@pytest.mark.parametrize(
    "reader,index", [(bin_storage.entries, ".bin.json"), (bin_storage.restored, ".bin-restored.json")]
)
@pytest.mark.parametrize("text,wanted", [("[]", {}), ("bad", {}), ('{"kept": 1, "ignored": "one"}', {"kept": 1})])
def test_bin_indexes_keep_only_integer_timestamps(files, reader, index, text, wanted):
    (core.LEDGER_DIR / index).write_text(text)
    assert reader() == wanted


def test_bin_index_encoding_sorting_and_default_restore_clock(files, monkeypatch):
    files.create("second", content())
    files.create("first", content())
    files.delete("second", now=2)
    files.delete("first", now=1)
    assert (core.LEDGER_DIR / ".bin.json").read_bytes() == b'{\n "first": 1,\n "second": 2\n}'
    monkeypatch.setattr(core, "now_ms", lambda: 3)
    assert files.restore("second")
    assert (core.LEDGER_DIR / ".bin-restored.json").read_bytes() == b'{\n "second": 3\n}'
    assert bin_storage.purge_expired(now=1 + 30 * 86400000) == []
    assert bin_storage.purge_expired(now=2 + 30 * 86400000) == ["first"]
    assert bin_storage.entries() == {}


def test_restore_mark_order_and_missing_files_survive_purge(files):
    files.create("z-last", content())
    files.create("a-first", content())
    files.delete("z-last", now=1)
    files.delete("a-first", now=2)
    assert files.restore("z-last", now=3)
    assert files.restore("a-first", now=4)
    assert (core.LEDGER_DIR / ".bin-restored.json").read_bytes() == b'{\n "a-first": 4,\n "z-last": 3\n}'
    files.delete("absent", now=1)
    files.delete("young", now=31 * 86400000)
    assert bin_storage.purge_expired(now=1 + 31 * 86400000) == ["absent"]
    assert bin_storage.entries() == {"young": 31 * 86400000}


def test_auto_bin_continues_after_an_unreadable_ledger(files):
    files.write_page("a-bad", "broken")
    files.create("z-good", content())
    files.apply_ops("z-good", changes=[{"path": "phases/p1/done", "value": True}])
    assert bin_storage.auto_bin(now=1) == ["z-good"]


def test_creation_makes_parent_folders_and_uses_the_requested_slug(files, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", core.LEDGER_DIR / "parent" / "nested")
    assert files.create("nested", content())
    assert '"nested"' in files.read_page("nested")
    assert files.exists("nested")
    core.paths("nested")[1].unlink()
    assert files.exists("nested")


def test_file_reads_keep_utf8_when_the_locale_is_ascii(files):
    import locale
    import sys

    if sys.flags.utf8_mode:
        pytest.skip("UTF8 mode overrides the locale encoding")
    files.create("utf8", content())
    files.apply_ops("utf8", changes=[{"path": "phases/p1/done", "value": True}])
    files.create("seed-only", content())
    core.paths("seed-only")[1].unlink()
    (core.LEDGER_DIR / ".bin.json").write_text('{"Café": 1}', encoding="utf-8")
    (core.LEDGER_DIR / ".bin-restored.json").write_text('{"Café": 1}', encoding="utf-8")
    before = locale.setlocale(locale.LC_CTYPE)
    locale.setlocale(locale.LC_CTYPE, "C")
    try:
        assert files.get_document("utf8")["title"] == "Café"
        assert files.read_snapshot("utf8")["overview"] == "Résumé"
        assert "Café" in files.read_page("utf8")
        summaries = files.list_summaries()
        assert summaries[0]["title"] == "Café"
        assert {row["slug"] for row in summaries} == {"seed-only", "utf8"}
        assert bin_storage.entries() == {"Café": 1}
        assert bin_storage.restored() == {"Café": 1}
        assert bin_storage.auto_bin(now=1) == ["utf8"]
    finally:
        locale.setlocale(locale.LC_CTYPE, before)
