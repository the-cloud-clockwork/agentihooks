from concurrent.futures import ThreadPoolExecutor

import pytest

from scripts.swarm_ledger.repository import FileLedgerRepository
from scripts.swarm_ledger.repository.file import core
from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    repo = FileLedgerRepository()
    repo.create(
        "shadow", {"title": "Shadow", "overview": "o", "sources": [], "phases": [{"title": "One", "description": "d"}]}
    )
    return repo


def test_every_mutation_has_equal_documents_events_and_bin(files):
    shadow = SQLiteLedgerRepository(core.LEDGER_DIR / "ledger-shadow.sqlite3", files)
    assert shadow.get_document("shadow", reconcile=False) == files.get_document("shadow", reconcile=False)
    before = files.get_document("shadow")
    op = {"op": "add", "thread": "phases/p1/comments", "text": "Comment", "id": "comment"}
    state, rejected = files.apply_ops("shadow", ops=[op])
    assert rejected == []
    assert shadow.get_document("shadow", reconcile=False) == state
    assert shadow.events_since("shadow", before["_meta"]["rev"]) == files.events_since("shadow", before["_meta"]["rev"])
    duplicate, rejected = shadow.apply_ops("shadow", ops=[op])
    assert duplicate == state
    assert rejected == []
    files.delete("shadow", now=12)
    assert shadow.lifecycle("shadow")["deleted_at"] == 12
    assert files.restore("shadow", now=15)
    assert shadow.lifecycle("shadow") == {"deleted_at": None, "restored_at": 15}
    assert shadow.get_document("shadow", reconcile=False) == files.get_document("shadow", reconcile=False)


def test_concurrent_mutations_leave_equal_ordered_records(files):
    def join(index):
        return files.apply_ops("shadow", ops=[{"op": "join", "by": f"eng{index}", "id": f"join{index}"}])

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(join, range(8)))
    assert all(not rejected for _, rejected in results)
    state = files.get_document("shadow", reconcile=False)
    assert all(f"eng{index}" in state["_meta"]["members"] for index in range(8))
    assert SQLiteLedgerRepository(core.LEDGER_DIR / "ledger-shadow.sqlite3").get_document("shadow") == state


def test_file_commit_recovers_after_shadow_failure(files, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(SQLiteLedgerRepository, "verify", lambda *args: (_ for _ in ()).throw(RuntimeError("crash")))
        with pytest.raises(RuntimeError, match="crash"):
            files.apply_ops("shadow", ops=[{"op": "join", "id": "crash", "by": "eng"}])
    state = files.get_document("shadow")
    assert "eng" in state["_meta"]["members"]
    assert SQLiteLedgerRepository(core.LEDGER_DIR / "ledger-shadow.sqlite3").get_document("shadow") == state


def test_stale_changes_keep_conflict_and_shadow_equality(files):
    change = {"path": "phases/p1/done", "base": False, "value": True}
    state, rejected = files.apply_ops("shadow", changes=[change])
    assert rejected == []
    stale, rejected = files.apply_ops("shadow", changes=[{**change, "value": False}])
    assert rejected == ["phases/p1/done"]
    assert stale["phases"][0]["done"] is True
    assert SQLiteLedgerRepository(core.LEDGER_DIR / "ledger-shadow.sqlite3").get_document("shadow") == stale


def test_sqlite_repository_routes_lifecycle_to_authoritative_files(files):
    shadow = SQLiteLedgerRepository(core.LEDGER_DIR / "ledger-shadow.sqlite3", files)
    content = {"title": "Extra", "overview": "o", "sources": [], "phases": [{"title": "One", "description": "d"}]}
    assert shadow.create("extra", content, size="small") is True
    assert shadow.create("extra", content) is False
    assert shadow.list_summaries() == files.list_summaries()
    assert shadow.get_document("extra") == files.get_document("extra")
    shadow.delete("extra", now=10)
    assert shadow.lifecycle("extra")["deleted_at"] == 10
    assert shadow.restore("extra", now=20) is True
    assert shadow.restore("extra", now=30) is False
    assert shadow.lifecycle("extra") == {"deleted_at": None, "restored_at": 20}


def test_unchanged_file_skips_shadow_reconstruction(files, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(
            SQLiteLedgerRepository,
            "verify",
            lambda *args: (_ for _ in ()).throw(RuntimeError("unexpected reconstruction")),
        )
        files.get_document("shadow")
        with pytest.raises(RuntimeError, match="unexpected reconstruction"):
            files.apply_ops("shadow", ops=[{"op": "join", "id": "changed", "by": "eng"}])
    files.get_document("shadow")
    assert SQLiteLedgerRepository(core.LEDGER_DIR / "ledger-shadow.sqlite3").get_document(
        "shadow"
    ) == files.get_document("shadow", reconcile=False)
