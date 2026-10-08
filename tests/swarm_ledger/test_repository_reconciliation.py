import json

import ledger_core as core
import pytest
from scripts.swarm_ledger.repository.file import FileLedgerRepository

from tests.swarm_ledger.test_repository_files import content


@pytest.fixture
def stored(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    repo = FileLedgerRepository()
    repo.create("reconcile", content())
    return repo


def seed_page(repo, seed):
    repo.write_page(
        "reconcile",
        core.SEED_RE.sub(lambda m: m.group(1) + json.dumps(seed) + m.group(3), repo.read_page("reconcile"), count=1),
    )


def test_old_metadata_without_events_gets_an_empty_event_stream(stored):
    doc = stored.read_snapshot("reconcile")
    del doc["_meta"]["events"]
    core.paths("reconcile")[1].write_text(json.dumps(doc))
    assert stored.get_document("reconcile", reconcile=False)["_meta"]["events"] == []


def test_legacy_seed_omits_revision_notifications_and_phase_review(stored):
    seed = core.parse_seed(stored.read_page("reconcile"))
    seed["notifications"] = [{"id": "old"}]
    seed["phases"][0]["review"] = {"state": "old"}
    seed_page(stored, seed)
    core.paths("reconcile")[1].unlink()
    doc = stored.get_document("reconcile")
    assert "_rev" not in doc and doc["notifications"] == []
    assert doc["phases"][0].get("review") != {"state": "old"}


def test_unversioned_seed_uses_current_revision_and_merges_agent_threads(stored):
    seed = core.parse_seed(stored.read_page("reconcile"))
    del seed["_rev"]
    seed["phases"][0]["comments"].append({"id": "agent", "by": "eng", "at": 1, "text": "Agent note"})
    seed_page(stored, seed)
    doc = stored.get_document("reconcile")
    assert [row["text"] for row in doc["phases"][0]["comments"]] == ["Agent note"]
    assert any(event["by"] == "eng" for event in doc["_meta"]["events"])


def test_unknown_seed_revision_is_reported_without_losing_state(stored):
    seed = core.parse_seed(stored.read_page("reconcile"))
    seed["_rev"] = 999
    seed["overview"] = "Stale edit"
    seed_page(stored, seed)
    doc = stored.get_document("reconcile")
    assert doc["overview"] == "Résumé"
    assert doc["_meta"]["warnings"] == [
        "the page copy at revision 999 is too old to merge, its agent edits were ignored"
    ]


def test_rejected_changes_and_operations_both_survive_the_repository(stored):
    _, rejected = stored.apply_ops(
        "reconcile",
        changes=[{"path": "phases/missing/done", "value": True}],
        ops=[{"op": "add", "thread": "phases/missing/comments", "id": "missing", "text": "No thread"}],
    )
    assert rejected == ["phases/missing/done", "missing"]


def test_supplied_gate_can_refuse_an_operation(stored):
    class Gate:
        def apply(self, doc, op, ctx, apply):
            return False

    state, rejected = stored.apply_ops(
        "reconcile", ops=[{"op": "add", "thread": "chat", "id": "refused", "text": "No entry"}], gate=Gate()
    )
    assert rejected == ["refused"] and state["chat"] == []


def test_a_repeated_seed_error_keeps_the_revision_stable(stored):
    stored.write_page("reconcile", "broken page")
    first = stored.get_document("reconcile")
    second = stored.get_document("reconcile")
    assert second["_meta"] == first["_meta"]


def test_repository_changes_are_written_and_returned(stored):
    state, rejected = stored.apply_ops("reconcile", changes=[{"path": "phases/p1/done", "value": True}])
    assert rejected == [] and state["phases"][0]["done"] is True
    assert stored.get_document("reconcile", reconcile=False)["phases"][0]["done"] is True


def test_legacy_state_reader_reports_creation_and_canonical_metadata(stored):
    seed = core.parse_seed(stored.read_page("reconcile"))
    core.paths("reconcile")[1].unlink()
    _, meta, created = core.load_state(core.paths("reconcile")[1], seed)
    assert created is True
    assert meta["rev"] == 0
    assert set(meta) == {"rev", "stamps", "events", "seeds", "seed_error", "updated_at"}
    assert meta["seeds"].keys() == {"0"}
    with pytest.raises(ValueError, match="missing and the HTML seed is unreadable"):
        stored.get_document("reconcile", reconcile=False)
    restored = stored.get_document("reconcile")
    assert restored["_meta"]["rev"] == 1


def test_serialization_keeps_two_space_indentation(stored):
    assert core.paths("reconcile")[1].read_bytes().startswith(b'{\n  "size": "small",\n  "title": "Caf\xc3\xa9",\n')


def test_seed_history_keeps_exactly_five_versions(stored):
    for n in range(6):
        stored.apply_ops("reconcile", ops=[{"op": "add", "thread": "chat", "id": str(n), "text": "entry"}])
    meta = stored.get_document("reconcile")["_meta"]
    assert set(meta["seeds"]) == {"3", "4", "5", "6", "7"}
    assert set(meta) == {
        "rev",
        "stamps",
        "events",
        "seeds",
        "seed_error",
        "updated_at",
        "members",
        "created_at",
        "priorities_dismissed",
        "warnings",
    }


def test_package_core_clock_is_preserved_when_json_is_missing(stored, monkeypatch):
    from scripts.swarm_ledger import ledger_core as package_core

    monkeypatch.setattr(package_core, "LEDGER_DIR", core.LEDGER_DIR)
    monkeypatch.setattr(package_core, "now_ms", lambda: 1000)
    if package_core is not core:
        monkeypatch.setattr(core, "now_ms", lambda: 500)
    seed = core.parse_seed(stored.read_page("reconcile"))
    core.paths("reconcile")[1].unlink()
    _, meta, created = package_core.load_state(package_core.paths("reconcile")[1], seed)
    assert created is True and meta["updated_at"] == 1000
    repo = FileLedgerRepository(package_core)
    doc = repo.get_document("reconcile")
    assert doc["_meta"]["created_at"] == 1000


def test_events_require_a_persisted_document(stored):
    core.paths("reconcile")[1].unlink()
    with pytest.raises(ValueError, match="missing and the HTML seed is unreadable"):
        stored.events_since("reconcile", 0)


def test_core_loaded_under_another_name_keeps_its_clock(stored, monkeypatch):
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(core.__name__, core.__file__)
    alias = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, alias)
    spec.loader.exec_module(alias)
    monkeypatch.setattr(alias, "now_ms", lambda: 1000)
    monkeypatch.setattr(core, "now_ms", lambda: 500)
    seed = core.parse_seed(stored.read_page("reconcile"))
    core.paths("reconcile")[1].unlink()
    _, meta, created = alias.load_state(core.paths("reconcile")[1], seed)
    assert created is True and meta["updated_at"] == 1000
