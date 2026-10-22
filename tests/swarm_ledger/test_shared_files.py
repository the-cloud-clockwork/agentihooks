import hashlib
import sqlite3
from contextlib import contextmanager

import pytest

from scripts.swarm_ledger import ledger_artifacts, ledger_media, plan_ranges, shared_files
from scripts.swarm_ledger.repository import postgres
from scripts.swarm_ledger.repository.sqlite import DATABASE, SQLiteLedgerRepository
from scripts.swarm_ledger.storage_migration import hosted
from tests.swarm_ledger.test_media import png

MARKDOWN = b"# Shared\n\nOne store for every replica.\n"


def sqlite_files(path):
    @contextmanager
    def connect():
        connection = sqlite3.connect(path)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    return shared_files.Files(connect)


@pytest.fixture
def replicas(tmp_path, monkeypatch):
    files = sqlite_files(tmp_path / "shared.db")
    monkeypatch.setattr(shared_files, "shared", lambda: files)
    disks = {name: tmp_path / name for name in ("pod-0", "pod-1")}
    for disk in disks.values():
        disk.mkdir()

    def on(name):
        monkeypatch.setattr(ledger_media.core, "LEDGER_DIR", disks[name])

    return files, disks, on


def rows(files, slug):
    with files.session() as connection:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT name, touched FROM ledger_files WHERE slug = ? ORDER BY name", (slug,)
            )
        ]


def test_an_image_uploaded_through_one_replica_is_read_by_the_other(replicas):
    files, disks, on = replicas
    on("pod-0")
    image = png(4, 3)
    stored = ledger_media.store("shared", image)
    on("pod-1")
    assert ledger_media.read("shared", stored["id"]) == image
    assert ledger_media.entry("shared", stored["id"]) == stored
    assert [list(disk.iterdir()) for disk in disks.values()] == [[pod_lock(disks["pod-0"])], []]


def pod_lock(disk):
    return disk / ".ledger-media.lock"


def test_an_artifact_published_through_one_replica_is_read_by_the_other(replicas):
    _, _, on = replicas
    on("pod-0")
    stored = ledger_artifacts.store("shared", "notes.md", MARKDOWN)
    on("pod-1")
    assert ledger_artifacts.read("shared", stored["id"]) == MARKDOWN
    assert ledger_artifacts.entry("shared", stored["id"]) == stored


def test_a_plan_artifact_text_is_read_from_the_shared_store(replicas):
    _, _, on = replicas
    on("pod-0")
    stored = ledger_artifacts.store("shared", "plan.md", MARKDOWN)
    on("pod-1")
    doc = {"artifacts": [{"plan": True, "file": {"id": stored["id"]}}]}
    ref = {"artifact": f"https://ledger.test/artifacts/shared/{stored['id']}", "lines": "1-3"}
    assert plan_ranges.stored_text(ref, doc) == MARKDOWN.decode()


def test_a_missing_or_malformed_id_is_refused_by_the_shared_store(replicas):
    _, _, on = replicas
    on("pod-1")
    missing = "a" * 64 + ".png"
    with pytest.raises(ValueError, match=f"^no stored file {missing}$"):
        ledger_media.read("shared", missing)
    with pytest.raises(ValueError, match="^no stored file ../x.png$"):
        ledger_media.read("shared", "../x.png")
    with pytest.raises(ValueError, match="^no stored file 7$"):
        ledger_artifacts.read("shared", 7)


def test_a_second_upload_of_the_same_bytes_only_renews_its_time(replicas, monkeypatch):
    files, _, on = replicas
    on("pod-0")
    image = png()
    monkeypatch.setattr(ledger_media.time, "time", lambda: 100.0)
    media_id = ledger_media.store("shared", image)["id"]
    on("pod-1")
    monkeypatch.setattr(ledger_media.time, "time", lambda: 200.0)
    assert ledger_media.store("shared", image)["id"] == media_id
    assert rows(files, "shared") == [(media_id, 200.0)]
    assert files.get("shared", media_id) == image


def test_sweep_drop_and_purge_delete_from_the_shared_store(replicas, monkeypatch):
    files, _, on = replicas
    on("pod-0")
    monkeypatch.setattr(ledger_media.time, "time", lambda: 10.0)
    old_used = ledger_media.store("shared", png(1, 1))["id"]
    old_unused = ledger_media.store("shared", png(2, 1))["id"]
    monkeypatch.setattr(ledger_media.time, "time", lambda: 5000.0)
    fresh = ledger_media.store("shared", png(3, 1))["id"]
    other = ledger_media.store("other", png(2, 1))["id"]
    on("pod-1")
    ledger_media.sweep("shared", {old_used}, 3700_000)
    assert [name for name, _ in rows(files, "shared")] == sorted([old_used, fresh])
    ledger_media.drop("shared", {fresh})
    assert [name for name, _ in rows(files, "shared")] == [old_used]
    ledger_media.purge("shared")
    assert rows(files, "shared") == []
    assert [name for name, _ in rows(files, "other")] == [other]


def test_attachments_carry_no_pod_paths_from_the_shared_store(replicas):
    _, _, on = replicas
    on("pod-0")
    media_id = ledger_media.store("shared", png())["id"]
    doc = {"chat": [{"id": "c1", "attachments": [{"id": media_id}]}]}
    events = [{"target": "chat", "id": "c1"}]
    ledger_media.attach_paths("shared", doc, events)
    assert events == [{"target": "chat", "id": "c1"}]


def test_files_left_on_a_replica_disk_move_into_the_shared_store_once(tmp_path):
    files = sqlite_files(tmp_path / "shared.db")
    disk = tmp_path / "pod-0"
    folder = disk / "kept.media"
    folder.mkdir(parents=True)
    data = b"kept bytes"
    name = hashlib.sha256(data).hexdigest() + ".md"
    (folder / name).write_bytes(data)
    (folder / ("b" * 64 + ".md")).write_bytes(b"wrong content")
    (folder / "notes.txt").write_bytes(b"not content addressed")
    (folder / (hashlib.sha256(b"x").hexdigest() + ".md")).mkdir()
    (folder / (hashlib.sha256(data).hexdigest() + ".png")).symlink_to(folder / name)
    (disk / "loose.media").write_bytes(b"a file, not a folder")
    assert files.adopt(disk, 7.0) == 1
    assert files.adopt(disk, 9.0) == 0
    assert rows(files, "kept") == [(name, 7.0)]
    assert files.read_all("kept") == {name: data}
    assert (folder / name).read_bytes() == data


def test_the_shared_store_is_the_shared_database_and_absent_without_one(monkeypatch):
    shared_files.at.cache_clear()
    monkeypatch.delenv(postgres.URL, raising=False)
    assert shared_files.shared() is None
    monkeypatch.setenv(postgres.URL, "postgresql://ledger@db.test/ledger")
    files = shared_files.shared()
    assert files is shared_files.shared() is shared_files.of(postgres.PostgresLedgerRepository(files.address))
    assert files.address == "postgresql://ledger@db.test/ledger"
    assert shared_files.of(SQLiteLedgerRepository(None)) is None
    shared_files.at.cache_clear()


def test_the_schema_is_created_once_per_store(tmp_path):
    seen = []

    @contextmanager
    def connect():
        connection = sqlite3.connect(tmp_path / "shared.db")
        connection.set_trace_callback(seen.append)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    files = shared_files.Files(connect)
    files.get("s", "a")
    files.get("s", "b")
    assert [sql for sql in seen if sql.startswith("CREATE")] == [shared_files.SCHEMA]


def test_postgres_sessions_commit_and_return_their_connection(monkeypatch):
    made = []

    class Connection:
        def __init__(self, address):
            self.address, self.log = address, []
            made.append(self)

        def execute(self, sql, parameters=()):
            self.log.append(sql)

        def __enter__(self):
            return self

        def __exit__(self, kind, value, traceback):
            self.log.append("commit" if kind is None else "rollback")
            return False

        def close(self):
            self.log.append("close")

    monkeypatch.setattr(shared_files.postgres, "Connection", Connection)
    with shared_files.opened("postgresql://db.test/ledger") as connection:
        connection.execute("SELECT 1")
    with pytest.raises(KeyError), shared_files.opened("postgresql://db.test/ledger"):
        raise KeyError("x")
    assert [(entry.address, entry.log) for entry in made] == [
        ("postgresql://db.test/ledger", ["SELECT 1", "commit", "close"]),
        ("postgresql://db.test/ledger", ["rollback", "close"]),
    ]


def test_a_hosted_import_lands_files_in_the_shared_store_and_capture_reads_them(tmp_path, monkeypatch):
    from scripts.swarm_ledger import ledger_core
    from tests.swarm_ledger.test_sqlite import document

    source = SQLiteLedgerRepository(tmp_path / "source" / DATABASE, ledger_core)
    source.import_document("migration-proof", document())
    data = b"An artifact kept with its ledger."
    name = hashlib.sha256(data).hexdigest() + ".md"
    (source.directory / "migration-proof.media").mkdir()
    (source.directory / "migration-proof.media" / name).write_bytes(data)
    snapshot = hosted.capture(source, "migration-proof")
    files = sqlite_files(tmp_path / "shared.db")
    target = SQLiteLedgerRepository(tmp_path / "target" / DATABASE, ledger_core)
    monkeypatch.setattr(shared_files, "of", lambda repo: files if repo is target else None)
    assert hosted.restore(target, snapshot) == hosted.manifest(snapshot)
    assert files.read_all("migration-proof") == {name: data}
    assert not (target.directory / "migration-proof.media").exists()
    assert hosted.capture(target, "migration-proof") == snapshot
    files.put("migration-proof", name, b"changed", 1.0)
    with pytest.raises(ValueError, match="^hosted ledger conflict$"):
        hosted.restore(target, snapshot)


def test_a_hosted_import_refuses_a_shared_file_with_other_bytes(tmp_path, monkeypatch):
    files = sqlite_files(tmp_path / "shared.db")
    data = b"bytes"
    name = hashlib.sha256(data).hexdigest() + ".md"
    files.put("s", name, b"other", 1.0)
    with pytest.raises(ValueError, match="^hosted file conflict$"):
        hosted.write_shared(files, "s", {name: data})
    hosted.write_shared(files, "s", {})
    assert files.read_all("s") == {name: b"other"}
