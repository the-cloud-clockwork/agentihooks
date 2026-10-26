"""Staged session catalog migrations: additive schema, a resumable backfill, verification, then consumer activation."""

import json
from dataclasses import dataclass

from hooks.memory.catalog import ATTRIBUTED, ORIGINAL, UNKNOWN_PROJECT, Catalog, Conflict
from hooks.memory.index_worker import QUEUE

PROVENANCE = "0001-event-provenance"
DIMENSION = "0002-vector-dimension"
PENDING, ADDED, BACKFILLED, VERIFIED, ACTIVE = "pending", "added", "backfilled", "verified", "active"
OPERATOR = "operator"
LEDGER = """CREATE TABLE IF NOT EXISTS schema_migration (
    migration_id TEXT NOT NULL PRIMARY KEY, state TEXT NOT NULL, revision INTEGER NOT NULL,
    rows_before INTEGER NOT NULL, after_rowid INTEGER NOT NULL, rows_done INTEGER NOT NULL, report TEXT)"""
LEDGER_KEYS = ("state", "revision", "rows_before", "after_rowid", "rows_done", "report")
LEDGER_ROW = f"SELECT {', '.join(LEDGER_KEYS)} FROM schema_migration WHERE migration_id = ?"
INSERT_LEDGER = "INSERT INTO schema_migration VALUES (?, ?, 0, ?, 0, 0, NULL)"
ADVANCE = (
    "UPDATE schema_migration SET after_rowid = ?, rows_done = rows_done + ?, state = ?, revision = revision + 1 "
    "WHERE migration_id = ? AND revision = ?"
)
SET_STATE = (
    "UPDATE schema_migration SET state = ?, report = ?, revision = revision + 1 WHERE migration_id = ? AND revision = ?"
)
COUNT_EVENTS = "SELECT COUNT(*) FROM session_event"
MISSING = (
    "NOT EXISTS (SELECT 1 FROM session_event_provenance p "
    "WHERE p.access_domain = e.access_domain AND p.event_id = e.event_id)"
)
REMAINING = f"SELECT COUNT(*) FROM session_event e WHERE {MISSING}"
BATCH_END = "SELECT MAX(rowid) FROM (SELECT rowid FROM session_event WHERE rowid > ? ORDER BY rowid LIMIT ?)"
FILL = (
    "INSERT INTO session_event_provenance SELECT access_domain, event_id, ?, "
    "CASE WHEN project_id IS NULL THEN ? ELSE ? END FROM session_event e "
    f"WHERE rowid > ? AND rowid <= ? AND {MISSING} ON CONFLICT DO NOTHING"
)
CHECKS = {
    "migration_rows_remaining": REMAINING,
    "duplicate_event_sequences": (
        "SELECT COUNT(*) FROM (SELECT 1 FROM session_event "
        "GROUP BY access_domain, session_uid, source_generation, sequence HAVING COUNT(*) > 1)"
    ),
    "unresolved_sources": (
        "SELECT COUNT(*) FROM session_event e WHERE NOT EXISTS (SELECT 1 FROM session_source s "
        "WHERE s.access_domain = e.access_domain AND s.session_uid = e.session_uid "
        "AND s.source_generation = e.source_generation)"
    ),
    "orphan_provenance": (
        "SELECT COUNT(*) FROM session_event_provenance p WHERE NOT EXISTS (SELECT 1 FROM session_event e "
        "WHERE e.access_domain = p.access_domain AND e.event_id = p.event_id)"
    ),
    "inconsistent_provenance": (
        "SELECT COUNT(*) FROM session_event_provenance p JOIN session_event e "
        "ON e.access_domain = p.access_domain AND e.event_id = p.event_id "
        f"WHERE p.trust_class != '{ORIGINAL}' OR p.project_state != "
        f"CASE WHEN e.project_id IS NULL THEN '{UNKNOWN_PROJECT}' ELSE '{ATTRIBUTED}' END"
    ),
}
PROVENANCE_ROW = (
    "SELECT trust_class, project_state FROM session_event_provenance WHERE access_domain = ? AND event_id = ?"
)
INDEX_TABLES = (
    "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name IN ('chunk_vector', 'index_generation')"
)
MISMATCH_JOIN = (
    "FROM chunk_vector v JOIN index_generation g "
    "ON g.access_domain = v.access_domain AND g.index_generation = v.index_generation WHERE v.dimension != g.dimension"
)
MISMATCHES = (
    f"SELECT g.access_domain, g.index_generation, g.dimension, COUNT(*) {MISMATCH_JOIN} "
    "GROUP BY g.access_domain, g.index_generation, g.dimension ORDER BY g.access_domain, g.index_generation"
)
MISMATCHED = (
    f"SELECT v.chunk_id, v.session_uid, v.chunker_version, v.content_hash {MISMATCH_JOIN} "
    "AND v.access_domain = ? AND v.index_generation = ? ORDER BY v.chunk_id"
)
DROP_VECTOR = "DELETE FROM chunk_vector WHERE access_domain = ? AND index_generation = ? AND chunk_id = ?"
STALE_CURSOR = "the migration cursor revision is stale; nothing changed"
NOT_ADDED = "the migration schema has not been added"
NOT_BACKFILLED = "only a backfilled migration can be verified"
NOT_VERIFIED = "only a verified migration with no rows remaining can be activated"
NOT_ACTIVE = "only an active migration can be deactivated"
ACTIVE_BACKFILL = "deactivate the migration before backfilling it again"
INACTIVE = "the event provenance projection is not active"
BAD_BATCH = "a backfill needs a positive batch size and batch count"
UNAUTHORIZED = "a destructive vector migration needs an operator approval naming it and its index generation"


@dataclass(frozen=True)
class Approval:
    actor: str
    role: str
    migration_id: str
    access_domain: str
    index_generation: str


class Migrator:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog, self.db = catalog, catalog.db
        self.db.execute(LEDGER)

    def _ledger(self) -> dict | None:
        row = self.db.execute(LEDGER_ROW, (PROVENANCE,)).fetchone()
        return dict(zip(LEDGER_KEYS, row)) if row else None

    def _count(self, query: str) -> int:
        return self.db.execute(query).fetchone()[0]

    def _require(self, revision: int, states: tuple[str, ...], refusal: str) -> dict:
        found = self._ledger()
        if found is None:
            raise Conflict(NOT_ADDED)
        if found["revision"] != revision:
            raise Conflict(STALE_CURSOR)
        if found["state"] not in states:
            raise Conflict(refusal)
        return found

    def _set(self, state: str, revision: int, report: dict | None) -> None:
        encoded = None if report is None else json.dumps(report, sort_keys=True)
        if self.db.execute(SET_STATE, (state, encoded, PROVENANCE, revision)).rowcount != 1:
            raise Conflict(STALE_CURSOR)

    def add(self) -> bool:
        with self.catalog._transaction():
            if self._ledger() is not None:
                return False
            self.db.execute(INSERT_LEDGER, (PROVENANCE, ADDED, self._count(COUNT_EVENTS)))
            return True

    def status(self) -> dict:
        found = self._ledger() or {"state": PENDING}
        report = found.get("report")
        return {
            **found,
            "migration_id": PROVENANCE,
            "report": json.loads(report) if report else None,
            "migration_rows_remaining": self._count(REMAINING),
        }

    def _fill(self, after: int, through: int) -> int:
        return self.db.execute(FILL, (ORIGINAL, UNKNOWN_PROJECT, ATTRIBUTED, after, through)).rowcount

    def _step(self, revision: int, batch_size: int) -> tuple[int, bool]:
        with self.catalog._transaction():
            found = self._require(revision, (ADDED, BACKFILLED, VERIFIED), ACTIVE_BACKFILL)
            after = found["after_rowid"]
            through = self.db.execute(BATCH_END, (after, batch_size)).fetchone()[0]
            if through is None:
                if self._count(REMAINING) == 0:
                    self._set(BACKFILLED, revision, None)
                    return revision + 1, True
                through, filled = 0, 0
            else:
                filled = self._fill(after, through)
            if self.db.execute(ADVANCE, (through, filled, ADDED, PROVENANCE, revision)).rowcount != 1:
                raise Conflict(STALE_CURSOR)
            return revision + 1, False

    def backfill(self, revision: int, batch_size: int, max_batches: int) -> dict:
        if min(batch_size, max_batches) < 1:
            raise ValueError(BAD_BATCH)
        for _ in range(max_batches):
            revision, finished = self._step(revision, batch_size)
            if finished:
                break
        return self.status()

    def verify(self, revision: int) -> dict:
        with self.catalog._transaction():
            found = self._require(revision, (BACKFILLED, VERIFIED), NOT_BACKFILLED)
            report = {name: self._count(query) for name, query in CHECKS.items()}
            report["rows_before"], report["rows_now"] = found["rows_before"], self._count(COUNT_EVENTS)
            report["rows_lost"] = max(0, report["rows_before"] - report["rows_now"])
            passed = not any(report[name] for name in (*CHECKS, "rows_lost"))
            self._set(VERIFIED if passed else BACKFILLED, revision, {**report, "passed": passed})
        return self.status()

    def activate(self, revision: int) -> dict:
        with self.catalog._transaction():
            found = self._require(revision, (VERIFIED,), NOT_VERIFIED)
            if self._count(REMAINING):
                raise Conflict(NOT_VERIFIED)
            self._set(ACTIVE, revision, json.loads(found["report"]))
        return self.status()

    def deactivate(self, revision: int) -> dict:
        with self.catalog._transaction():
            found = self._require(revision, (ACTIVE,), NOT_ACTIVE)
            self._set(VERIFIED, revision, json.loads(found["report"]))
        return self.status()

    def provenance(self, domain: str, event_id: str) -> dict | None:
        if (self._ledger() or {}).get("state") != ACTIVE:
            raise Conflict(INACTIVE)
        row = self.db.execute(PROVENANCE_ROW, (domain, event_id)).fetchone()
        return {"trust_class": row[0], "project_state": row[1]} if row else None

    def blocked(self) -> list[dict]:
        if self._count(INDEX_TABLES) != 2:
            return []
        keys = ("access_domain", "index_generation", "dimension", "mismatched_vectors")
        rows = self.db.execute(MISMATCHES).fetchall()
        return [{"migration_id": DIMENSION, **dict(zip(keys, row)), "requires": OPERATOR} for row in rows]

    def startup(self) -> dict:
        return {"added": [PROVENANCE] if self.add() else [], "status": self.status(), "blocked": self.blocked()}

    def drop_mismatched_vectors(self, approval: Approval, now: float) -> dict:
        if approval.role != OPERATOR or not approval.actor or approval.migration_id != DIMENSION:
            raise Conflict(UNAUTHORIZED)
        index = (approval.access_domain, approval.index_generation)
        if self._count(INDEX_TABLES) != 2:
            return {"dropped": [], "requeued": 0}
        with self.catalog._transaction():
            rows = self.db.execute(MISMATCHED, index).fetchall()
            for chunk_id, session_uid, chunker_version, content_hash in rows:
                self.db.execute(DROP_VECTOR, (*index, chunk_id))
                self.db.execute(QUEUE, (*index, chunk_id, session_uid, chunker_version, content_hash, now))
        return {"dropped": [row[0] for row in rows], "requeued": len(rows)}
