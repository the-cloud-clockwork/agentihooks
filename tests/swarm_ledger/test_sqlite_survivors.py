import json

import ledger_artifacts
import ledger_core
import pytest

from scripts.swarm_ledger.repository import legacy, sqlite
from scripts.swarm_ledger.repository.sqlite import Missing, read_ledger, scope
from tests.swarm_ledger.test_sqlite import CONTENT, chat, document, store


def generation(repo, slug):
    with repo.connect() as connection:
        return connection.execute(sqlite.GENERATION, (slug,)).fetchone()[0]


def fresh(warnings=None):
    doc, meta, _ = legacy.fresh(__import__("new_ledger").build_doc(CONTENT))
    if warnings is not None:
        meta["warnings"] = warnings
    return doc, meta


@pytest.fixture
def swept(monkeypatch):
    slugs = []
    monkeypatch.setattr(ledger_artifacts, "sweep", lambda slug, doc, ctx: slugs.append(slug))
    return slugs


@pytest.mark.parametrize(
    "call",
    [
        lambda repo: repo.read("absent", "title"),
        lambda repo: repo.get_document("absent"),
        lambda repo: repo.events_since("absent", 0),
    ],
)
def test_a_missing_ledger_error_names_its_slug(tmp_path, call):
    repo = store(tmp_path)
    repo.import_document("present", document())
    with pytest.raises(Missing) as raised:
        call(repo)
    assert raised.value.args == ("absent",)


def test_a_first_insert_takes_the_clock_generation_and_a_replace_rises_above_it(tmp_path, monkeypatch):
    repo = store(tmp_path)
    monkeypatch.setattr(ledger_core, "now_ms", lambda: 5)
    repo.import_document("gen", document())
    assert generation(repo, "gen") == 5 << sqlite.GENERATION_SHIFT
    repo.import_document("gen", document(), replace=True)
    assert generation(repo, "gen") == (5 << sqlite.GENERATION_SHIFT) + 1
    monkeypatch.setattr(ledger_core, "now_ms", lambda: 6)
    repo.import_document("gen", document(), replace=True)
    assert generation(repo, "gen") == 6 << sqlite.GENERATION_SHIFT


def test_stored_rows_hold_an_empty_event_list_and_the_events_live_in_their_table(tmp_path):
    repo = store(tmp_path)
    repo.import_document("ev", document())
    exact, prefix = scope(["_meta", "events"])
    with repo.connect() as connection:
        rows = [
            (path, kind, value)
            for table in sqlite.TABLES
            for path, kind, value in connection.execute(
                f"SELECT path, kind, value FROM {table} WHERE slug=? AND (path=? OR (path > ? AND path < ?))",
                ("ev", exact[-1], prefix, prefix + sqlite.PATH_END),
            )
        ]
    assert rows == [(exact[-1], "array", "null")]
    assert repo.events_since("ev", 0) == document()["_meta"]["events"]


def test_an_import_without_a_token_gets_a_fresh_32_character_token(tmp_path):
    repo = store(tmp_path)
    repo.import_document("tok", document())
    assert len(repo.token("tok")) == 32


def test_create_keeps_the_size_it_was_asked_for(tmp_path):
    repo = store(tmp_path)
    assert repo.create("sized", CONTENT, "swarm") is True
    assert repo.export_document("sized")["size"] == "swarm"


def test_create_document_refuses_a_stored_slug_and_leaves_it_unchanged(tmp_path):
    repo = store(tmp_path)
    assert repo.create_document("made", *fresh()) is True
    before = repo.export_document("made")
    doc, meta = fresh()
    doc["title"] = "Other"
    assert repo.create_document("made", doc, meta) is False
    assert repo.export_document("made") == before


def test_create_document_counts_its_creation_as_the_first_revision(tmp_path):
    repo = store(tmp_path)
    assert repo.create_document("born", *fresh(warnings=[])) is True
    assert repo.export_document("born")["_meta"]["rev"] == 1


def test_create_document_and_writes_derive_artifacts_for_their_own_slug(tmp_path, swept):
    repo = store(tmp_path)
    repo.create_document("swept", *fresh())
    repo.apply_ops("swept", ops=[chat(1)])
    assert swept == ["swept", "swept"]


def test_a_write_adopts_only_its_own_legacy_file(tmp_path):
    repo = store(tmp_path)
    repo.create("mine", CONTENT)
    other = repo.directory / "other.json"
    other.write_text(json.dumps(document()), encoding="utf-8")
    repo.apply_ops("mine", ops=[chat(1)])
    assert other.exists()
    assert read_ledger(repo.directory, "other", "title") is None


def test_registries_are_saved_to_the_registry_table(tmp_path):
    repo = store(tmp_path)
    with repo.connect() as connection, connection:
        connection.execute(sqlite.BEGIN_IMMEDIATE)
        repo.save_registry(connection, "bin", {"gone": 3})
    with repo.connect() as connection:
        assert connection.execute("SELECT slug, path, value FROM registry").fetchall() == [("bin", "gone", "3")]


def test_sync_through_the_full_module_reads_and_writes_that_modules_folder(tmp_path, monkeypatch):
    from scripts.swarm_ledger import ledger_core as full
    from scripts.swarm_ledger.repository import repository

    monkeypatch.setattr(full, "LEDGER_DIR", tmp_path)
    assert repository.bound(full).create("full", CONTENT) is True
    bound = []
    real = repository.bound
    monkeypatch.setattr(repository, "bound", lambda domain: bound.append(domain) or real(domain))
    state, rejected = full.sync("full", ops=[chat(1)])
    assert bound == [full]
    assert (rejected, [message["id"] for message in state["chat"]]) == ([], ["m1"])
