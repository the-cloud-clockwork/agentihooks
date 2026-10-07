from concurrent.futures import ThreadPoolExecutor

import pytest

from scripts.swarm_ledger.repository import FileLedgerRepository
from scripts.swarm_ledger.repository.file import core
from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    monkeypatch.setenv("LEDGER_SQLITE_SHADOW", "1")
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


def test_bin_registry_and_automatic_expiry_equal_files(files):
    from scripts.swarm_ledger.repository import bin_storage

    shadow = SQLiteLedgerRepository(core.LEDGER_DIR / "ledger-shadow.sqlite3")
    assert bin_storage.bin_closed("shadow", 1, now=12)
    assert shadow.lifecycle("shadow")["deleted_at"] == 12
    assert shadow.registry("bin") == bin_storage.registries()["bin"]
    assert files.restore("shadow", now=15)
    assert shadow.registry("restored") == bin_storage.registries()["restored"]
    files.delete("shadow", now=20)
    assert shadow.registry("bin") == bin_storage.registries()["bin"]
    assert bin_storage.purge_expired(now=20 + 31 * bin_storage.domain.DAY_MS) == ["shadow"]
    assert shadow.registry("bin") == bin_storage.registries()["bin"]
    with pytest.raises(KeyError):
        shadow.get_document("shadow")
    with pytest.raises(KeyError):
        shadow.events_since("shadow", 0)


def test_idle_ledgers_are_binned_with_equal_shadow_records(files):
    from scripts.swarm_ledger.repository import bin_storage

    state = files.get_document("shadow", reconcile=False)
    now = state["_meta"]["updated_at"] + 8 * bin_storage.domain.DAY_MS
    assert bin_storage.auto_bin(now=now) == ["shadow"]
    shadow = SQLiteLedgerRepository(core.LEDGER_DIR / "ledger-shadow.sqlite3")
    assert shadow.registry("bin") == bin_storage.registries()["bin"]
    assert shadow.lifecycle("shadow")["deleted_at"] == now
    assert shadow.get_document("shadow") == state


@pytest.mark.parametrize("recovery", ["import", "expiry"])
def test_interrupted_purge_reconciles_absent_shadow_on_restart(files, monkeypatch, recovery):
    from scripts.swarm_ledger.repository import bin_storage, shadow
    from scripts.swarm_ledger.storage_migration import import_directory

    files.delete("shadow", now=20)
    now = 20 + 31 * bin_storage.domain.DAY_MS
    with monkeypatch.context() as patch:
        patch.setattr(shadow, "persist_lifecycle", lambda *args: (_ for _ in ()).throw(RuntimeError("crash")))
        with pytest.raises(RuntimeError, match="crash"):
            bin_storage.purge_expired(now=now)
    assert not core.paths("shadow")[1].exists()
    if recovery == "import":
        assert import_directory(core.LEDGER_DIR) == []
    else:
        assert bin_storage.purge_expired(now=now) == []
    repo = SQLiteLedgerRepository(core.LEDGER_DIR / "ledger-shadow.sqlite3")
    with pytest.raises(KeyError):
        repo.get_document("shadow")
    assert repo.registry("bin") == bin_storage.registries()["bin"]


def test_sqlite_changes_operations_and_gate_keep_file_semantics(files):
    from types import SimpleNamespace

    shadow = SQLiteLedgerRepository(core.LEDGER_DIR / "ledger-shadow.sqlite3", files)
    op = {"op": "add", "thread": "phases/p1/comments", "text": "New comment", "id": "new"}
    state, rejected = shadow.apply_ops(
        "shadow",
        changes=[{"path": "phases/p1/done", "value": True}],
        ops=[op],
        gate=SimpleNamespace(apply=lambda *args: False),
    )
    assert state["phases"][0]["done"] is True
    assert state["phases"][0]["comments"] == []
    assert rejected == ["new"]
    state, rejected = shadow.apply_ops("shadow", ops=[op])
    assert rejected == []
    assert state["phases"][0]["comments"][0]["text"] == "New comment"
    assert shadow.get_document("shadow", reconcile=False) == state


def test_lifecycle_metadata_survives_document_mutations_and_new_import(files):
    from scripts.swarm_ledger.repository import bin_storage

    files.delete("shadow", now=12)
    files.restore("shadow", now=15)
    files.delete("shadow", now=16)
    state, rejected = files.apply_ops("shadow", ops=[{"op": "join", "id": "newjoin", "by": "eng"}])
    assert rejected == []
    repo = SQLiteLedgerRepository(core.LEDGER_DIR / "ledger-shadow.sqlite3")
    assert repo.lifecycle("shadow") == {"deleted_at": 16, "restored_at": 15}
    assert repo.registry("bin") == bin_storage.registries()["bin"]
    assert repo.registry("restored") == bin_storage.registries()["restored"]
    imported = SQLiteLedgerRepository(core.LEDGER_DIR / "lifecycle-import.sqlite3")
    imported.apply_lifecycle(core.LEDGER_DIR, bin_storage.registries())
    assert imported.get_document("shadow") == state
    assert imported.lifecycle("shadow") == {"deleted_at": 16, "restored_at": 15}


def test_changed_authoritative_file_at_same_revision_is_reimported(files):
    state = files.get_document("shadow", reconcile=False)
    state["extension"] = {"data": True}
    core.atomic_write(core.paths("shadow")[1], core.json.dumps(state))
    reconciled = files.get_document("shadow")
    assert reconciled["_meta"]["rev"] == state["_meta"]["rev"]
    assert SQLiteLedgerRepository(core.LEDGER_DIR / "ledger-shadow.sqlite3").get_document("shadow") == reconciled


def test_seed_read_uses_retained_snapshot_without_file_reconciliation(files, monkeypatch):
    repo = SQLiteLedgerRepository(core.LEDGER_DIR / "ledger-shadow.sqlite3", files)
    state = repo.get_document("shadow", reconcile=False)
    revision = str(state["_meta"]["rev"])
    monkeypatch.setattr(
        files, "get_document", lambda *args: (_ for _ in ()).throw(RuntimeError("unexpected reconcile"))
    )
    assert repo.get_seed("shadow", revision) == state["_meta"]["seeds"][revision]


def test_changed_bin_metadata_is_verified_during_document_reconciliation(files):
    index = {"future": {"text": "é"}}
    core.atomic_write(core.LEDGER_DIR / ".bin.json", core.json.dumps(index, ensure_ascii=False))
    files.get_document("shadow")
    assert SQLiteLedgerRepository(core.LEDGER_DIR / "ledger-shadow.sqlite3").registry("bin") == index


def test_default_document_read_reconciles_authoritative_changes(files):
    state = files.get_document("shadow", reconcile=False)
    state["extension"] = {"read": True}
    core.atomic_write(core.paths("shadow")[1], core.json.dumps(state))
    repo = SQLiteLedgerRepository(core.LEDGER_DIR / "ledger-shadow.sqlite3", files)
    assert repo.get_document("shadow")["extension"] == {"read": True}


def test_sqlite_creation_preserves_size_and_default(files):
    repo = SQLiteLedgerRepository(core.LEDGER_DIR / "ledger-shadow.sqlite3", files)
    content = {"title": "Size", "overview": "o", "sources": [], "phases": [{"title": "One", "description": "d"}]}
    assert repo.create("default-size", content)
    assert repo.get_document("default-size")["size"] == "small"
    assert repo.create("full-size", content, size="swarm")
    assert repo.get_document("full-size")["size"] == "swarm"


def test_shadow_registry_and_lifecycle_reads_preserve_unicode_in_ascii_locale(files):
    import locale
    import sys

    from scripts.swarm_ledger.repository import bin_storage

    if sys.flags.utf8_mode:
        pytest.skip("UTF8 mode overrides the locale encoding")
    index = {"future": {"text": "é"}}
    core.atomic_write(core.LEDGER_DIR / ".bin.json", core.json.dumps(index, ensure_ascii=False))
    before = locale.setlocale(locale.LC_CTYPE)
    locale.setlocale(locale.LC_CTYPE, "C")
    try:
        assert bin_storage.registries()["bin"] == index
        files.get_document("shadow")
        assert SQLiteLedgerRepository(core.LEDGER_DIR / "ledger-shadow.sqlite3").registry("bin") == index
    finally:
        locale.setlocale(locale.LC_CTYPE, before)
