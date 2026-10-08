import json
import os
import re
import types
from pathlib import Path

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


def test_a_stray_file_for_a_stored_ledger_is_set_aside_without_replacing_it(tmp_path, capsys):
    directory = folder(tmp_path)
    repo = stored(directory)
    repo.import_document("same", document())
    stray = json.dumps({**document(), "title": "Stray"})
    (directory / "same.json").write_text(stray, encoding="utf-8")
    assert repo.get_document("same")["title"] == "Café"
    assert not (directory / "same.json").exists()
    assert [path.read_text(encoding="utf-8") for path in (directory / legacy.BACKUP).glob("same-*/same.json")] == [
        stray
    ]
    assert capsys.readouterr().err == "legacy ledger same is already stored; its files were set aside in .imported\n"


def test_an_unreadable_file_is_kept_and_reported_on_a_sweep_but_refused_by_name(tmp_path, capsys):
    directory = folder(tmp_path)
    (directory / "broken.json").write_text("{not json", encoding="utf-8")
    repo = stored(directory)
    assert repo.list_summaries() == []
    assert "legacy ledger broken not imported" in capsys.readouterr().err
    assert (directory / "broken.json").exists()
    with pytest.raises(ValueError):
        repo.get_document("broken")


def test_an_unreadable_file_is_backed_up_and_reported_once_until_it_changes(tmp_path, capsys):
    directory = folder(tmp_path)
    broken = directory / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    repo = stored(directory)
    for _ in range(3):
        repo.list_summaries()
    assert len(list((directory / legacy.BACKUP).iterdir())) == 1
    assert capsys.readouterr().err.count("legacy ledger broken not imported") == 1
    with pytest.raises(ValueError):
        repo.get_document("broken")
    broken.write_text(json.dumps(document()), encoding="utf-8")
    assert repo.get_document("broken")["title"] == "Café"


def test_bin_indexes_merge_into_the_registry_and_are_set_aside(tmp_path):
    directory = folder(tmp_path)
    (directory / ".bin.json").write_text(json.dumps({"gone": 5}), encoding="utf-8")
    (directory / ".bin-restored.json").write_text("not json", encoding="utf-8")
    repo = stored(directory)
    repo.list_summaries()
    assert sqlite.read_registry(directory, "bin") == {"gone": 5}
    assert sqlite.read_registry(directory, "restored") == {}
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


class FakePath:
    def __init__(self, text="", exists=True):
        self.text = text
        self._exists = exists
        self.encodings = []

    def exists(self):
        return self._exists

    def read_text(self, encoding=None):
        self.encodings.append(encoding)
        return self.text

    def __str__(self):
        return "fake.json"


def test_load_state_reads_the_json_file_as_utf8():
    path = FakePath(json.dumps({"_meta": {"rev": 1}}))
    core_stub = types.SimpleNamespace(loads=json.loads, normalize=lambda s: s)
    legacy.load_state(path, None, core=core_stub)
    assert path.encodings == ["utf-8"]


def test_load_state_rejects_meta_that_is_a_list_even_when_it_contains_rev():
    path = FakePath(json.dumps({"_meta": ["rev"]}))
    core_stub = types.SimpleNamespace(loads=json.loads, normalize=lambda s: s)
    with pytest.raises(ValueError, match="has no ledger _meta"):
        legacy.load_state(path, None, core=core_stub)


def test_load_state_defaults_missing_events_to_an_empty_list():
    path = FakePath(json.dumps({"_meta": {"rev": 2}}))
    core_stub = types.SimpleNamespace(loads=json.loads, normalize=lambda s: s)
    _, meta, created = legacy.load_state(path, None, core=core_stub)
    assert meta == {"rev": 2, "events": []}
    assert created is False


def test_load_state_raises_when_the_file_is_missing_and_there_is_no_seed():
    path = FakePath(exists=False)
    with pytest.raises(ValueError, match="is missing and the HTML seed is unreadable"):
        legacy.load_state(path, None, core=types.SimpleNamespace())


def test_load_state_forwards_its_core_argument_into_fresh_when_the_file_is_missing():
    path = FakePath(exists=False)
    marker = object()
    fake_core = types.SimpleNamespace(normalize=lambda seed: {"phases": [], "marker": marker}, now_ms=lambda: 0)
    doc, meta, created = legacy.load_state(path, {"phases": []}, core=fake_core)
    assert doc["marker"] is marker
    assert created is True


def test_fresh_strips_rev_and_notifications_and_each_phases_review():
    fake_core = types.SimpleNamespace(
        normalize=lambda seed: {
            "_rev": 9,
            "notifications": ["n"],
            "phases": [{"id": "p1", "review": "pending", "title": "T"}],
            "title": "Doc",
        },
        now_ms=lambda: 555,
    )
    doc, meta, created = legacy.fresh({}, core=fake_core)
    assert doc == {"title": "Doc", "phases": [{"id": "p1", "title": "T"}]}
    assert meta == {"rev": 0, "stamps": {}, "events": [], "updated_at": 555}
    assert created is True


def test_import_files_reads_the_html_page_as_utf8(tmp_path, monkeypatch):
    directory = folder(tmp_path)
    seed = new_ledger.build_doc({"title": "Fresh", "overview": "o", "phases": []})
    (directory / "enc.html").write_text(page(seed), encoding="utf-8")
    repo = stored(directory)
    calls = []
    original_read_text = Path.read_text

    def spy(self, *args, **kwargs):
        if self.name == "enc.html":
            calls.append(kwargs.get("encoding"))
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy)
    legacy.import_files(repo, "enc", [directory / "enc.html"])
    assert calls == ["utf-8"]


def test_import_files_never_replaces_a_stored_ledger(tmp_path):
    directory = folder(tmp_path)
    repo = stored(directory)
    first = new_ledger.build_doc({"title": "First", "overview": "o", "phases": []})
    (directory / "dup.html").write_text(page(first), encoding="utf-8")
    legacy.import_files(repo, "dup", [directory / "dup.html"])
    assert repo.get_document("dup")["title"] == "First"
    second = new_ledger.build_doc({"title": "Second", "overview": "o2", "phases": []})
    (directory / "dup.html").write_text(page(second), encoding="utf-8")
    legacy.import_files(repo, "dup", [directory / "dup.html"])
    assert repo.get_document("dup")["title"] == "First"
    (directory / "dup.json").write_text(json.dumps(document()), encoding="utf-8")
    with pytest.raises(ValueError, match="exists"):
        legacy.import_files(repo, "dup", [directory / "dup.json"])


def test_adopt_registries_processes_restored_even_when_bin_file_is_absent(tmp_path):
    directory = folder(tmp_path)
    (directory / ".bin-restored.json").write_text(json.dumps({"r": 1}), encoding="utf-8")
    repo = stored(directory)
    legacy.adopt_registries(repo)
    assert sqlite.read_registry(directory, "restored") == {"r": 1}
    assert not (directory / ".bin-restored.json").exists()


def test_adopt_registries_reads_registry_files_as_utf8(tmp_path, monkeypatch):
    directory = folder(tmp_path)
    (directory / ".bin.json").write_text(json.dumps({"a": 1}), encoding="utf-8")
    repo = stored(directory)
    calls = []
    original_read_text = Path.read_text

    def spy(self, *args, **kwargs):
        if self.name == ".bin.json":
            calls.append(kwargs.get("encoding"))
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy)
    legacy.adopt_registries(repo)
    assert calls == ["utf-8"]


def test_adopt_registries_backs_up_under_the_exact_stripped_registry_name(tmp_path):
    directory = folder(tmp_path)
    (directory / ".bin.json").write_text(json.dumps({"a": 1}), encoding="utf-8")
    (directory / ".bin-restored.json").write_text(json.dumps({"b": 2}), encoding="utf-8")
    repo = stored(directory)
    legacy.adopt_registries(repo)
    names = [p.name for p in (directory / legacy.BACKUP).iterdir()]
    assert any(re.fullmatch(r"bin-\d+-\d+", name) for name in names)
    assert any(re.fullmatch(r"restored-\d+-\d+", name) for name in names)


def test_adopt_registries_merges_new_entries_over_what_is_already_stored(tmp_path):
    directory = folder(tmp_path)
    repo = stored(directory)
    with repo.connect() as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        repo.save_registry(connection, "bin", {"already-there": 10})
    (directory / ".bin.json").write_text(json.dumps({"from-file": 20}), encoding="utf-8")
    legacy.adopt_registries(repo)
    assert sqlite.read_registry(directory, "bin") == {"already-there": 10, "from-file": 20}


def test_adopt_bin_imports_bin_files_under_the_folder_storage_lock(tmp_path, monkeypatch):
    directory = folder(tmp_path)
    (directory / ".bin-restored.json").write_text(json.dumps({"r": 1}), encoding="utf-8")
    repo = stored(directory)
    locked = []
    real = legacy.storage_lock
    monkeypatch.setattr(legacy, "storage_lock", lambda path: locked.append(path) or real(path))
    legacy.adopt_bin(repo)
    assert locked == [directory]
    assert sqlite.read_registry(directory, "restored") == {"r": 1}
    assert not (directory / ".bin-restored.json").exists()


def test_adopt_bin_without_bin_files_takes_no_lock(tmp_path, monkeypatch):
    directory = folder(tmp_path)
    monkeypatch.setattr(legacy, "storage_lock", lambda path: pytest.fail("no bin file to adopt"))
    legacy.adopt_bin(stored(directory))


def test_candidates_requires_actual_files_for_a_named_slug(tmp_path):
    directory = folder(tmp_path)
    assert legacy.candidates(directory, "wanted") == []


def test_candidates_discovers_html_only_legacy_files_when_none_is_requested(tmp_path):
    directory = folder(tmp_path)
    seed = new_ledger.build_doc({"title": "T", "overview": "o", "phases": []})
    (directory / "htmlonly.html").write_text(page(seed), encoding="utf-8")
    assert legacy.candidates(directory, None) == ["htmlonly"]


def test_adopt_with_an_explicit_slug_leaves_other_legacy_files_alone(tmp_path):
    directory = folder(tmp_path)
    repo = stored(directory)
    wanted_seed = new_ledger.build_doc({"title": "Wanted", "overview": "o", "phases": []})
    other_seed = new_ledger.build_doc({"title": "Other", "overview": "o", "phases": []})
    (directory / "wanted.html").write_text(page(wanted_seed), encoding="utf-8")
    (directory / "other.html").write_text(page(other_seed), encoding="utf-8")
    legacy.adopt(repo, "wanted")
    assert repo.exists("wanted")
    assert (directory / "other.html").exists()
    with repo.connect() as connection:
        assert connection.execute("SELECT 1 FROM ledgers WHERE slug=?", ("other",)).fetchone() is None


def test_adopt_skips_a_candidate_whose_files_vanished_but_keeps_processing_others(tmp_path, monkeypatch):
    directory = folder(tmp_path)
    repo = stored(directory)
    seed = new_ledger.build_doc({"title": "Bbb", "overview": "o", "phases": []})
    (directory / "aaa.html").write_text(page(seed), encoding="utf-8")
    (directory / "bbb.html").write_text(page(seed), encoding="utf-8")
    real_files = legacy.files

    def fake_files(dir_, name):
        if name == "aaa":
            return []
        return real_files(dir_, name)

    monkeypatch.setattr(legacy, "files", fake_files)
    legacy.adopt(repo)
    assert not (directory / "bbb.html").exists()
    assert (directory / "aaa.html").exists()


def test_adopt_continues_past_a_cached_refusal_to_a_later_candidate(tmp_path):
    directory = folder(tmp_path)
    repo = stored(directory)
    (directory / "mmm.json").write_text("{not json", encoding="utf-8")
    legacy.adopt(repo)
    seed = new_ledger.build_doc({"title": "Zzz", "overview": "o", "phases": []})
    (directory / "zzz.html").write_text(page(seed), encoding="utf-8")
    legacy.adopt(repo)
    assert not (directory / "zzz.html").exists()


def test_adopt_continues_past_a_fresh_bad_file_to_a_later_candidate(tmp_path):
    directory = folder(tmp_path)
    repo = stored(directory)
    (directory / "mmm.json").write_text("{not json", encoding="utf-8")
    seed = new_ledger.build_doc({"title": "Zzz", "overview": "o", "phases": []})
    (directory / "zzz.html").write_text(page(seed), encoding="utf-8")
    legacy.adopt(repo)
    assert not (directory / "zzz.html").exists()


def test_import_once_tracks_failures_by_the_actual_path_not_a_fixed_marker(tmp_path):
    directory = folder(tmp_path)
    repo = stored(directory)
    a = directory / "a.json"
    b = directory / "b.json"
    a.write_text("{not json", encoding="utf-8")
    b.write_text("{not json", encoding="utf-8")
    stat_a = a.stat()
    os.utime(b, ns=(stat_a.st_atime_ns, stat_a.st_mtime_ns))
    with pytest.raises(ValueError):
        legacy.import_once(repo, "a", [a])
    with pytest.raises(ValueError) as exc_info:
        legacy.import_once(repo, "b", [b])
    assert not isinstance(exc_info.value, legacy.Repeated)


def test_import_once_repeats_the_cached_refusal_message(tmp_path):
    directory = folder(tmp_path)
    repo = stored(directory)
    bad = directory / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        legacy.import_once(repo, "bad", [bad])
    with pytest.raises(legacy.Repeated, match="was refused before"):
        legacy.import_once(repo, "bad", [bad])


def test_create_uses_small_as_its_default_size(tmp_path):
    directory = folder(tmp_path)
    repo = stored(directory)
    content = {"title": "Def", "overview": "o", "phases": [{"title": "P", "description": "d"}]}
    assert legacy.create(repo, "defsize", content) is True
    assert repo.get_document("defsize")["size"] == "small"


def test_create_forwards_an_explicit_size_into_the_built_document(tmp_path):
    directory = folder(tmp_path)
    repo = stored(directory)
    content = {"title": "Swarm", "overview": "o", "phases": [{"title": "P", "description": "d"}]}
    assert legacy.create(repo, "swarmsize", content, size="swarm") is True
    assert repo.get_document("swarmsize")["size"] == "swarm"


def test_create_refuses_an_existing_slug_without_validating_content(tmp_path):
    directory = folder(tmp_path)
    repo = stored(directory)
    content = {"title": "First", "overview": "o", "phases": [{"title": "P", "description": "d"}]}
    legacy.create(repo, "dup", content)
    assert legacy.create(repo, "dup", {}) is False


def test_create_rejects_invalid_content_with_an_exact_message(tmp_path):
    directory = folder(tmp_path)
    repo = stored(directory)
    with pytest.raises(SystemExit) as exc_info:
        legacy.create(repo, "bad", {"title": "", "overview": "o", "phases": []})
    assert str(exc_info.value) == "content rejected:\n  title is empty\n  no phases"


def test_create_stores_the_built_document_under_its_slug(tmp_path):
    directory = folder(tmp_path)
    repo = stored(directory)
    content = {"title": "Real One", "overview": "ov", "phases": [{"title": "P1", "description": "d"}]}
    assert legacy.create(repo, "realone", content) is True
    doc = repo.get_document("realone")
    assert doc["title"] == "Real One"
    assert doc["phases"][0]["title"] == "P1"
    assert doc["size"] == "small"
