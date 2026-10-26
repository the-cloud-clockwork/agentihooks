"""Garbage collection: abandoned uploads and retired index generations go only with ownership and grace proven."""

import argparse
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from hooks.memory import archive
from hooks.memory.api.ingest import Grant
from hooks.memory.catalog import ArchiveEntry, Catalog, Conflict
from hooks.memory.index_worker import IndexWorker

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS upload_reservation (
        access_domain TEXT NOT NULL, upload_id TEXT NOT NULL, object_digest TEXT NOT NULL, attempt TEXT NOT NULL,
        expires_at REAL NOT NULL, state TEXT NOT NULL, PRIMARY KEY (access_domain, upload_id))""",
    """CREATE TABLE IF NOT EXISTS index_retirement (
        access_domain TEXT NOT NULL, index_generation TEXT NOT NULL, retired_at REAL NOT NULL,
        PRIMARY KEY (access_domain, index_generation))""",
)
GC_TABLES = ("upload_reservation", "index_retirement")
RESERVED, COLLECTING = "reserved", "collecting"
BYTES = "garbage_collection_bytes"
REJECTIONS = "unsafe_delete_rejections"
RESERVATION = (
    "SELECT object_digest, attempt, expires_at, state FROM upload_reservation WHERE access_domain = ? AND upload_id = ?"
)
INSERT_RESERVATION = "INSERT INTO upload_reservation VALUES (?, ?, ?, ?, ?, ?)"
DIGEST_STATES = "SELECT access_domain, expires_at, state FROM upload_reservation WHERE object_digest = ?"
RESERVED_DIGESTS = "SELECT DISTINCT object_digest FROM upload_reservation WHERE access_domain = ?"
RELEASE = "DELETE FROM upload_reservation WHERE access_domain = ? AND upload_id = ?"
CLAIM = "UPDATE upload_reservation SET state = ? WHERE object_digest = ?"
DROP_DIGEST = "DELETE FROM upload_reservation WHERE object_digest = ?"
SETTLE = "DELETE FROM upload_reservation WHERE object_digest = ? AND state = ?"
ARCHIVED = "SELECT 1 FROM archive_manifest WHERE object_digest = ? LIMIT 1"
ALL_ARCHIVED = "SELECT DISTINCT object_digest FROM archive_manifest"
RETIREMENT = "SELECT retired_at FROM index_retirement WHERE access_domain = ? AND index_generation = ?"
RETIRE = "INSERT INTO index_retirement VALUES (?, ?, ?) ON CONFLICT DO NOTHING"
RETIRED = "SELECT index_generation, retired_at FROM index_retirement WHERE access_domain = ? ORDER BY index_generation"
INDEX_KEY = "access_domain = ? AND index_generation = ?"
REQUEUED = f"SELECT 1 FROM embedding_work WHERE {INDEX_KEY} AND queued_at > ? LIMIT 1"
INDEX_BYTES = f"SELECT COALESCE(SUM(LENGTH(vector)), 0) FROM chunk_vector WHERE {INDEX_KEY}"
DROP_INDEX = tuple(
    f"DELETE FROM {table} WHERE {INDEX_KEY}"
    for table in ("chunk_vector", "embedding_work", "index_generation", "index_retirement")
)
BAD_DIGEST = "an upload reservation must name a sha256 hex digest"
UNDER_COLLECTION = "this object is being collected; reserve it again after the collection finishes"
RESERVATION_CONFLICT = "this upload id is already reserved for different bytes or another attempt"
UNKNOWN_UPLOAD = "no reservation is held under this upload id; nothing was committed"
WRONG_OBJECT = "the archive entry names an object other than the one reserved; nothing was committed"
FOREIGN_ENTRY = "the archive entry belongs to another access domain than the grant; nothing was committed"
RETIRE_ACTIVE = "the active index generation cannot be retired"


@dataclass(frozen=True)
class Plan:
    grant: Grant
    references: Callable[[], frozenset[str]]
    grace: float
    now: float
    destructive: bool = True


@dataclass
class Report:
    destructive: bool
    reclaimed_bytes: int = 0
    rejections: int = 0
    uploads: list[str] = field(default_factory=list)
    generations: list[str] = field(default_factory=list)
    protected: dict[str, list[str]] = field(
        default_factory=lambda: {
            name: []
            for name in ("authoritative", "live", "rebound", "unowned", "altered", "active_index", "index_in_use")
        }
    )

    def keep(self, reason: str, name: str, rejected: bool = False) -> None:
        self.protected[reason].append(name)
        self.rejections += rejected

    def as_dict(self) -> dict:
        return {
            "mode": "collect" if self.destructive else "inventory",
            "committed": self.destructive,
            BYTES: self.reclaimed_bytes,
            REJECTIONS: self.rejections,
            "reclaimed": {"uploads": sorted(self.uploads), "index_generations": sorted(self.generations)},
            "protected": {reason: sorted(names) for reason, names in self.protected.items()},
        }


def _schema(catalog: Catalog) -> None:
    for statement in SCHEMA:
        catalog.db.execute(statement)


class Reservations:
    """Upload attempts record their object before its bytes land, so only an expired reservation proves ownership."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        _schema(catalog)

    def reserve(self, grant: Grant, upload_id: str, digest: str, attempt: str, expires_at: float) -> dict:
        if not archive.DIGEST.fullmatch(digest):
            raise ValueError(BAD_DIGEST)
        row = (grant.corpus, upload_id, digest, attempt, expires_at, RESERVED)
        with self.catalog._transaction():
            if any(state == COLLECTING for _, _, state in self.catalog.db.execute(DIGEST_STATES, (digest,))):
                raise Conflict(UNDER_COLLECTION)
            found = self.catalog.db.execute(RESERVATION, (grant.corpus, upload_id)).fetchone()
            if found is None:
                self.catalog.db.execute(INSERT_RESERVATION, row)
            elif tuple(found) != row[2:]:
                raise Conflict(RESERVATION_CONFLICT)
        return {"upload_id": upload_id, "object_digest": digest, "expires_at": expires_at}

    def commit(self, grant: Grant, upload_id: str, store: archive.ObjectStore, entry: ArchiveEntry) -> bool:
        """Releases the reservation, then acknowledges the entry; the bytes must still verify or nothing is archived."""
        if entry.access_domain != grant.corpus:
            raise Conflict(FOREIGN_ENTRY)
        with self.catalog._transaction():
            found = self.catalog.db.execute(RESERVATION, (grant.corpus, upload_id)).fetchone()
            if found is None or found[3] != RESERVED:
                raise Conflict(UNKNOWN_UPLOAD)
            if found[0] != entry.object_digest:
                raise Conflict(WRONG_OBJECT)
            self.catalog.db.execute(RELEASE, (grant.corpus, upload_id))
        return archive.acknowledge(self.catalog, store, entry)


def retire(catalog: Catalog, grant: Grant, generation: str, now: float) -> float:
    """Marks a non-active index generation retired; the first retirement time is kept on a replay."""
    _schema(catalog)
    worker = IndexWorker(catalog)
    with catalog._transaction():
        worker.model(grant.corpus, generation)
        if worker.active(grant.corpus)["index_generation"] == generation:
            raise Conflict(RETIRE_ACTIVE)
        catalog.db.execute(RETIRE, (grant.corpus, generation, now))
        return catalog.db.execute(RETIREMENT, (grant.corpus, generation)).fetchone()[0]


def _managed(store: archive.ObjectStore, path: Path) -> str | None:
    name = path.name
    if archive.DIGEST.fullmatch(name) and path.parent.name == name[:2] and path.parent.parent == store.root:
        return name
    return None


def _files(store: archive.ObjectStore) -> tuple[dict[str, Path], list[str]]:
    found = sorted(path for path in store.root.rglob("*") if path.is_file()) if store.root.exists() else []
    managed, foreign = {}, []
    for path in found:
        digest = _managed(store, path)
        if digest:
            managed[digest] = path
        else:
            foreign.append(path.relative_to(store.root).as_posix())
    return managed, foreign


def _referenced(catalog: Catalog, plan: Plan, digest: str) -> bool:
    return catalog.db.execute(ARCHIVED, (digest,)).fetchone() is not None or digest in plan.references()


def _owner(catalog: Catalog, plan: Plan, digest: str) -> str | None:
    """The protection reason for a digest, or None when every reservation on it is ours and expired past grace."""
    rows = catalog.db.execute(DIGEST_STATES, (digest,)).fetchall()
    if not rows or any(domain != plan.grant.corpus for domain, _, _ in rows):
        return "unowned"
    if any(state == RESERVED and expires_at + plan.grace > plan.now for _, expires_at, state in rows):
        return "live"
    return None


def _claim(catalog: Catalog, plan: Plan, digest: str) -> str | None:
    with catalog._transaction():
        if _referenced(catalog, plan, digest):
            catalog.db.execute(DROP_DIGEST, (digest,))
            return "rebound"
        reason = _owner(catalog, plan, digest)
        if reason is None:
            catalog.db.execute(CLAIM, (COLLECTING, digest))
        return reason


def _intact(path: Path, digest: str) -> bool:
    return hashlib.sha256(path.read_bytes()).hexdigest() == digest


def _settle(catalog: Catalog, digest: str) -> bool:
    """Ends a crashed run's claim on an object that is referenced again; a live attempt's reservation stays."""
    with catalog._transaction():
        return catalog.db.execute(SETTLE, (digest, COLLECTING)).rowcount > 0


def _collect_upload(catalog: Catalog, plan: Plan, digest: str, path: Path | None, report: Report) -> None:
    size = path.stat().st_size if path else 0
    if path and not _intact(path, digest):
        report.keep("altered", digest, rejected=True)
        return
    if not plan.destructive:
        report.uploads.append(digest)
        report.reclaimed_bytes += size
        return
    reason = _claim(catalog, plan, digest)
    if reason is not None:
        report.keep(reason, digest, rejected=reason == "unowned")
        return
    rebound = digest in plan.references()
    if path and not rebound:
        path.unlink(missing_ok=True)
    with catalog._transaction():
        catalog.db.execute(DROP_DIGEST, (digest,))
    if rebound:
        report.keep("rebound", digest)
        return
    report.uploads.append(digest)
    report.reclaimed_bytes += size


def _uploads(catalog: Catalog, store: archive.ObjectStore, plan: Plan, report: Report) -> None:
    files, foreign = _files(store)
    for name in foreign:
        report.keep("unowned", name, rejected=True)
    archived = {row[0] for row in catalog.db.execute(ALL_ARCHIVED)}
    reserved = {row[0] for row in catalog.db.execute(RESERVED_DIGESTS, (plan.grant.corpus,))}
    pinned = archived | plan.references()
    for name in sorted(files.keys() | reserved):
        if name in pinned:
            report.keep("rebound" if plan.destructive and _settle(catalog, name) else "authoritative", name)
            continue
        reason = _owner(catalog, plan, name)
        if reason is not None:
            report.keep(reason, name, rejected=reason == "unowned")
            continue
        _collect_upload(catalog, plan, name, files.get(name), report)


def _drop_generation(catalog: Catalog, plan: Plan, generation: str, retired_at: float) -> str | int:
    domain = plan.grant.corpus
    key = (domain, generation)
    with catalog._transaction():
        if IndexWorker(catalog).active(domain)["index_generation"] == generation:
            return "active_index"
        if catalog.db.execute(REQUEUED, (*key, retired_at)).fetchone() is not None:
            return "index_in_use"
        size = catalog.db.execute(INDEX_BYTES, key).fetchone()[0]
        if plan.destructive:
            for statement in DROP_INDEX:
                catalog.db.execute(statement, key)
        return size


def _generations(catalog: Catalog, plan: Plan, report: Report) -> None:
    for generation, retired_at in catalog.db.execute(RETIRED, (plan.grant.corpus,)).fetchall():
        if retired_at + plan.grace > plan.now:
            report.keep("live", generation)
            continue
        outcome = _drop_generation(catalog, plan, generation, retired_at)
        if isinstance(outcome, str):
            report.keep(outcome, generation)
            continue
        report.generations.append(generation)
        report.reclaimed_bytes += outcome


def collect(catalog: Catalog, store: archive.ObjectStore, plan: Plan) -> dict:
    """Reclaims abandoned uploads and retired index generations; a rerun after a crash resumes claimed objects.

    With destructive off it is a read only inventory of what a collection would reclaim.
    """
    _schema(catalog)
    IndexWorker(catalog)
    report = Report(plan.destructive)
    _uploads(catalog, store, plan, report)
    _generations(catalog, plan, report)
    return report.as_dict()


def main(argv: list[str] | None = None, clock: Callable[[], float] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("inventory", "collect", "retire"))
    parser.add_argument("catalog")
    parser.add_argument("domain")
    parser.add_argument("--principal", default="operator")
    parser.add_argument("--objects", type=Path)
    parser.add_argument("--references", type=Path)
    parser.add_argument("--grace", type=float, default=86400.0)
    parser.add_argument("--generation")
    args = parser.parse_args(argv)
    if (args.command == "retire") != (args.generation is not None):
        parser.error("--generation is given with retire and only with it")
    if (args.command != "retire") != (args.objects is not None):
        parser.error("--objects is given with inventory and collect and only with them")
    now = time.time() if clock is None else clock()
    grant = Grant(args.principal, args.domain, frozenset())
    catalog = Catalog(args.catalog)
    if args.command == "retire":
        answer = {"index_generation": args.generation, "retired_at": retire(catalog, grant, args.generation, now)}
    else:
        pinned = frozenset(json.loads(args.references.read_bytes())) if args.references else frozenset()
        plan = Plan(grant, lambda: pinned, args.grace, now, args.command == "collect")
        answer = collect(catalog, archive.ObjectStore(args.objects), plan)
    catalog.close()
    print(json.dumps(answer))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
