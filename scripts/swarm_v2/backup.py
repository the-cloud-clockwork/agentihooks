"""Backup sets: one coordinated manifest of durable state, accepted only once an isolated restore drill verifies it."""

import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from hooks.memory import archive, chunking, index_worker
from hooks.memory.api.ingest import Grant
from hooks.memory.catalog import Catalog
from hooks.memory.chunking import ChunkIndex, ChunkScope, Policy
from hooks.memory.embeddings import Embedder
from hooks.memory.index_worker import IndexWorker, Target

FORMAT = "1.0"
METRIC = "backup_restore_verification_status"
UPLOADED, VERIFIED, PARTIAL, FAILED = "uploaded", "verified", "partial", "failed"
RESTORE_VERIFIED = (VERIFIED, PARTIAL)
PRESENT = "present"
CATALOG = "catalog"
FILES = ("ledger", "coordination", "brain")
PROJECTIONS = chunking.CHUNK_TABLES + index_worker.INDEX_TABLES
CATALOG_FILE = "catalog.sqlite3"
MARKER = "restore.json"
ARCHIVED = "SELECT DISTINCT object_digest FROM archive_manifest ORDER BY object_digest"
RETAINED = "SELECT session_uid FROM session_record WHERE access_domain = ? AND state = 'retained' ORDER BY session_uid"
UNSUPPORTED = "the backup format major version is not supported by this release; nothing was restored"
NO_KEY = "no encryption key is available for a backup component; nothing was restored"
NOT_ISOLATED = "the restore target holds other data; restore into an empty folder"
UNKNOWN_SET = "the backup set is not recorded"
STALE_DRILL = "a newer restore drill result is already recorded for this backup set; nothing changed"
UNRECOVERED = "a designated checkpoint object is missing from the backup set"


class Refused(Exception):
    pass


@dataclass(frozen=True)
class Sources:
    catalog: Path
    objects: archive.ObjectStore
    files: dict[str, bytes]
    checkpoints: dict[str, tuple[str, ...]]
    versions: dict[str, str]
    key_ids: dict[str, str]


@dataclass(frozen=True)
class Projections:
    grant: Grant
    policy: Policy
    index_generation: str
    embedder: Embedder


@dataclass(frozen=True)
class Drill:
    root: Path
    keyring: frozenset[str]
    projections: Projections
    query: Callable[[Catalog, archive.ObjectStore], dict]
    task_id: str
    clock: Callable[[], float]


@dataclass
class Restored:
    manifest: dict
    catalog: Catalog
    store: archive.ObjectStore
    irrecoverable: dict[str, str]


def _encoded(doc: dict) -> bytes:
    return json.dumps(doc, sort_keys=True).encode()


def _write(path: Path, data: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    Path(temporary).replace(path)


def _catalog_copy(path: Path) -> tuple[bytes, list[str]]:
    source, copy = sqlite3.connect(path), sqlite3.connect(":memory:")
    try:
        source.backup(copy)
        for table in PROJECTIONS:
            copy.execute(f"DROP TABLE IF EXISTS {table}")
        copy.commit()
        archived = [row[0] for row in copy.execute(ARCHIVED)]
        return copy.serialize(), archived
    finally:
        source.close()
        copy.close()


def _copied(source: archive.ObjectStore, backup: archive.ObjectStore, digest: str) -> str:
    try:
        backup.put(source.get(digest))
    except archive.EvidenceIntegrityError as error:
        return str(error)
    return PRESENT


def _component(backup: archive.ObjectStore, sources: Sources, name: str, data: bytes) -> dict:
    digest = backup.put(data)
    return {"digest": digest, "bytes": len(data), "version": sources.versions[name], "key_id": sources.key_ids[name]}


def capture(sources: Sources, backup: archive.ObjectStore, now: float) -> str:
    """Copies every source into the backup store and returns the set id, the digest of its manifest."""
    catalog, archived = _catalog_copy(sources.catalog)
    components = {CATALOG: _component(backup, sources, CATALOG, catalog)}
    for name in FILES:
        components[name] = _component(backup, sources, name, sources.files[name])
    inventory = set(archived).union(*sources.checkpoints.values())
    manifest = {
        "format": FORMAT,
        "captured_at": now,
        "components": components,
        "objects": {digest: _copied(sources.objects, backup, digest) for digest in sorted(inventory)},
        "checkpoints": {task: list(digests) for task, digests in sorted(sources.checkpoints.items())},
        "omitted_projections": list(PROJECTIONS),
    }
    return backup.put(_encoded(manifest))


def _admit(manifest: dict, keyring: frozenset[str]) -> None:
    if manifest["format"].split(".")[0] != FORMAT.split(".")[0]:
        raise Refused(UNSUPPORTED)
    if any(component["key_id"] not in keyring for component in manifest["components"].values()):
        raise Refused(NO_KEY)


def _claim(root: Path, set_id: str) -> None:
    marker = root / MARKER
    if root.exists() and any(root.iterdir()):
        if not marker.exists() or json.loads(marker.read_text())["set_id"] != set_id:
            raise Refused(NOT_ISOLATED)
        return
    root.mkdir(parents=True, exist_ok=True)
    _write(marker, _encoded({"set_id": set_id}))


def restore(backup: archive.ObjectStore, set_id: str, root: Path, keyring: frozenset[str]) -> Restored:
    """Rebuilds durable state in root from the backup store alone; a rerun into the same root resumes."""
    manifest = json.loads(backup.get(set_id))
    _admit(manifest, keyring)
    _claim(root, set_id)
    for name in FILES:
        _write(root / name, backup.get(manifest["components"][name]["digest"]))
    store, lost = archive.ObjectStore(root / "objects"), {}
    for digest, state in manifest["objects"].items():
        reason = state if state != PRESENT else _copied(backup, store, digest)
        if reason != PRESENT:
            lost[digest] = reason
    path = root / CATALOG_FILE
    if not path.exists():
        _write(path, backup.get(manifest["components"][CATALOG]["digest"]))
    return Restored(manifest, Catalog(path), store, lost)


def rebuild(catalog: Catalog, projections: Projections, now: float) -> dict:
    """Rebuilds the chunk and vector projections the backup omits from the restored source events."""
    grant, policy, generation = projections.grant, projections.policy, projections.index_generation
    chunks, worker = ChunkIndex(catalog), IndexWorker(catalog)
    worker.register(grant.corpus, generation, projections.embedder.model)
    sessions = [row[0] for row in catalog.db.execute(RETAINED, (grant.corpus,))]
    for uid in sessions:
        chunks.build(grant, ChunkScope(grant.corpus, uid), policy)
        worker.enqueue(grant, Target(grant.corpus, uid, policy.version, generation), now)
    drained = worker.drain(projections.embedder, grant.corpus, generation, now)
    return {"sessions": len(sessions), "state": drained["state"], "pending": drained["pending"]}


def _checkpoint(restored: Restored, task_id: str) -> dict:
    digests = restored.manifest["checkpoints"].get(task_id, [])
    missing = [digest for digest in digests if digest in restored.irrecoverable]
    for digest in digests:
        if digest not in missing:
            restored.store.get(digest)
    return {"task_id": task_id, "objects": len(digests), "recovered": bool(digests) and not missing}


def _status(answer: dict | None, checkpoint: dict, lost: dict) -> str:
    if answer is None or not checkpoint["recovered"]:
        return FAILED
    return PARTIAL if lost else VERIFIED


def drill(backup: archive.ObjectStore, set_id: str, plan: Drill) -> dict:
    """Restores into an isolated folder, rebuilds projections, answers the query and recovers the checkpoint."""
    started = plan.clock()
    try:
        restored = restore(backup, set_id, plan.root, plan.keyring)
    except archive.EvidenceIntegrityError as error:
        return {METRIC: FAILED, "reason": str(error), "recovery_time_seconds": plan.clock() - started}
    try:
        rebuilt = rebuild(restored.catalog, plan.projections, plan.clock())
        try:
            answer = plan.query(restored.catalog, restored.store)
        except (archive.EvidenceIntegrityError, archive.EvidenceRefused):
            answer = None
        checkpoint = _checkpoint(restored, plan.task_id)
    finally:
        restored.catalog.close()
    return {
        METRIC: _status(answer, checkpoint, restored.irrecoverable),
        "query_answered": answer is not None,
        "checkpoint": checkpoint,
        "rebuilt_projections": rebuilt,
        "irrecoverable": dict(sorted(restored.irrecoverable.items())),
        "recovery_point": restored.manifest["captured_at"],
        "recovery_time_seconds": plan.clock() - started,
    }


class Registry:
    """Backup set states; acceptance needs a recorded restore drill, never an upload alone."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def _save(self, sets: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _write(self.path, _encoded(sets))

    def record(self, set_id: str, captured_at: float) -> dict:
        sets = self._load()
        if set_id not in sets:
            sets[set_id] = {"status": UPLOADED, "captured_at": captured_at, "attempt": 0, "report": None}
            self._save(sets)
        return sets[set_id]

    def verify(self, set_id: str, attempt: int, report: dict) -> dict:
        sets = self._load()
        if set_id not in sets:
            raise Refused(UNKNOWN_SET)
        entry, digest = sets[set_id], hashlib.sha256(_encoded(report)).hexdigest()
        if attempt == entry["attempt"] and digest == entry["report"]:
            return entry
        if attempt <= entry["attempt"]:
            raise Refused(STALE_DRILL)
        sets[set_id] = {**entry, "status": report[METRIC], "attempt": attempt, "report": digest}
        self._save(sets)
        return sets[set_id]

    def accepted(self, set_id: str) -> bool:
        return self._load().get(set_id, {}).get("status") in RESTORE_VERIFIED

    def _newest(self, statuses: tuple[str, ...]) -> str | None:
        found = [(entry["captured_at"], key) for key, entry in self._load().items() if entry["status"] in statuses]
        return max(found)[1] if found else None

    def preserved(self) -> str | None:
        """The last independently verified set, which retention must never delete."""
        return self._newest(RESTORE_VERIFIED)

    def retention_allowed(self) -> bool:
        """Destructive retention runs only while the newest set restored with full coverage."""
        newest = self._newest((UPLOADED, VERIFIED, PARTIAL, FAILED))
        return newest is not None and self._load()[newest]["status"] == VERIFIED

    def status(self) -> dict[str, int]:
        counts = dict.fromkeys((UPLOADED, VERIFIED, PARTIAL, FAILED), 0)
        for entry in self._load().values():
            counts[entry["status"]] += 1
        return {METRIC: counts}
