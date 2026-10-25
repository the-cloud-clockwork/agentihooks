"""Durable session catalog: one SQLite transaction commits a batch's events, quarantine, watermark and response."""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS session_event (
        access_domain TEXT NOT NULL, event_id TEXT NOT NULL, session_uid TEXT NOT NULL,
        source_generation TEXT NOT NULL, sequence INTEGER NOT NULL, byte_start INTEGER NOT NULL,
        byte_end INTEGER NOT NULL, project_id TEXT, record TEXT NOT NULL, normalized_hash TEXT NOT NULL,
        PRIMARY KEY (access_domain, event_id),
        UNIQUE (access_domain, session_uid, source_generation, sequence))""",
    """CREATE TABLE IF NOT EXISTS session_quarantine (
        access_domain TEXT NOT NULL, session_uid TEXT NOT NULL, source_generation TEXT NOT NULL,
        sequence INTEGER NOT NULL, event_id TEXT NOT NULL, reason TEXT NOT NULL,
        PRIMARY KEY (access_domain, session_uid, source_generation, sequence))""",
    """CREATE TABLE IF NOT EXISTS session_ingest_batch (
        access_domain TEXT NOT NULL, batch_id TEXT NOT NULL, payload_digest TEXT NOT NULL, result TEXT NOT NULL,
        schema_version TEXT NOT NULL, PRIMARY KEY (access_domain, batch_id))""",
    """CREATE TABLE IF NOT EXISTS session_watermark (
        access_domain TEXT NOT NULL, session_uid TEXT NOT NULL, source_generation TEXT NOT NULL,
        durable_through INTEGER NOT NULL, schema_version TEXT NOT NULL,
        PRIMARY KEY (access_domain, session_uid, source_generation))""",
    """CREATE TABLE IF NOT EXISTS ingest_counter (
        access_domain TEXT NOT NULL, name TEXT NOT NULL, value INTEGER NOT NULL, PRIMARY KEY (access_domain, name))""",
    """CREATE TABLE IF NOT EXISTS session_record (
        access_domain TEXT NOT NULL, session_uid TEXT NOT NULL, provider TEXT NOT NULL, retention TEXT NOT NULL,
        state TEXT NOT NULL, revision INTEGER NOT NULL, PRIMARY KEY (access_domain, session_uid))""",
    """CREATE TABLE IF NOT EXISTS session_source (
        access_domain TEXT NOT NULL, session_uid TEXT NOT NULL, source_generation TEXT NOT NULL, origin TEXT NOT NULL,
        PRIMARY KEY (access_domain, session_uid, source_generation))""",
    """CREATE TABLE IF NOT EXISTS archive_manifest (
        access_domain TEXT NOT NULL, session_uid TEXT NOT NULL, source_generation TEXT NOT NULL,
        byte_start INTEGER NOT NULL, byte_end INTEGER NOT NULL, object_digest TEXT NOT NULL,
        PRIMARY KEY (access_domain, session_uid, source_generation, byte_start))""",
)
TABLES = (
    "session_event",
    "session_quarantine",
    "session_ingest_batch",
    "session_watermark",
    "ingest_counter",
    "session_record",
    "session_source",
    "archive_manifest",
)
DUPLICATES = "ingest_duplicate_events_total"
ARCHIVE_FAILURES = "archive_integrity_failures_total"
SESSION_STATES = ("retained", "restricted", "deleted")
RETENTIONS = ("retain-until-deleted", "retain-until-expiry")
UNKNOWN_RETENTION = "the session retention must be retain-until-deleted or retain-until-expiry"
OVERLAP_CONFLICT = "an archive object overlapping this range was recorded with different bounds"
SESSION_CONFLICT = "the session was registered with another provider or retention"
SOURCE_CONFLICT = "the source generation was registered with another origin"
STALE_REVISION = "the session is unknown or its revision is stale; nothing changed"
UNKNOWN_STATE = "the session state must be retained, restricted or deleted"
UNREGISTERED = "the source generation is not registered"
ARCHIVE_CONFLICT = "an archive object at this offset was recorded with different content"
SESSION_KEY = "access_domain = ? AND session_uid = ?"
SESSION = f"SELECT provider, retention, state, revision FROM session_record WHERE {SESSION_KEY}"
INSERT_SESSION = "INSERT INTO session_record VALUES (?, ?, ?, ?, ?, 1)"
SET_STATE = f"UPDATE session_record SET state = ?, revision = revision + 1 WHERE {SESSION_KEY} AND revision = ?"
SOURCE = f"SELECT origin FROM session_source WHERE {SESSION_KEY} AND source_generation = ?"
INSERT_SOURCE = "INSERT INTO session_source VALUES (?, ?, ?, ?)"
SOURCES = f"SELECT source_generation, origin FROM session_source WHERE {SESSION_KEY} ORDER BY source_generation"
ENTRY = (
    "SELECT byte_end, object_digest FROM archive_manifest "
    f"WHERE {SESSION_KEY} AND source_generation = ? AND byte_start = ?"
)
OVERLAP = (
    f"SELECT 1 FROM archive_manifest WHERE {SESSION_KEY} AND source_generation = ? AND byte_start < ? AND byte_end > ?"
)
INSERT_ENTRY = "INSERT INTO archive_manifest VALUES (?, ?, ?, ?, ?, ?)"
MANIFEST = (
    "SELECT byte_start, byte_end, object_digest FROM archive_manifest "
    f"WHERE {SESSION_KEY} AND source_generation = ? ORDER BY byte_start"
)
PRAGMAS = ("PRAGMA journal_mode=WAL", "PRAGMA synchronous=FULL", "PRAGMA busy_timeout=5000")
SCOPE = "access_domain = ? AND session_uid = ? AND source_generation = ?"
COUNT = (
    "INSERT INTO ingest_counter VALUES (?, ?, ?) "
    "ON CONFLICT (access_domain, name) DO UPDATE SET value = value + excluded.value"
)
SETTLED = (
    f"SELECT 'event', event_id, normalized_hash, project_id FROM session_event WHERE {SCOPE} AND sequence = ? "
    f"UNION ALL SELECT 'quarantine', event_id, reason, NULL FROM session_quarantine WHERE {SCOPE} AND sequence = ?"
)
INSERT_EVENT = "INSERT INTO session_event VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
INSERT_QUARANTINE = "INSERT INTO session_quarantine VALUES (?, ?, ?, ?, ?, ?)"
WATERMARK = f"SELECT durable_through FROM session_watermark WHERE {SCOPE}"
CONTIGUOUS = (
    f"WITH settled(sequence) AS (SELECT sequence FROM session_event WHERE {SCOPE} "
    f"UNION SELECT sequence FROM session_quarantine WHERE {SCOPE}) "
    "SELECT MIN(sequence) FROM (SELECT ? AS sequence UNION SELECT sequence FROM settled WHERE sequence > ?) "
    "WHERE sequence + 1 NOT IN (SELECT sequence FROM settled)"
)
ADVANCE = (
    "INSERT INTO session_watermark VALUES (?, ?, ?, ?, ?) "
    "ON CONFLICT (access_domain, session_uid, source_generation) "
    "DO UPDATE SET durable_through = excluded.durable_through, schema_version = excluded.schema_version"
)
INSERT_BATCH = "INSERT INTO session_ingest_batch VALUES (?, ?, ?, ?, ?)"
BATCH = "SELECT payload_digest, result FROM session_ingest_batch WHERE access_domain = ? AND batch_id = ?"
EVENTS = (
    "SELECT event_id, sequence, byte_start, byte_end, project_id, record, normalized_hash FROM session_event "
    f"WHERE {SCOPE} ORDER BY sequence"
)
QUARANTINED = f"SELECT sequence, event_id, reason FROM session_quarantine WHERE {SCOPE} ORDER BY sequence"
COUNTER = "SELECT value FROM ingest_counter WHERE access_domain = ? AND name = ?"


class Conflict(Exception):
    pass


@dataclass(frozen=True)
class StoredEvent:
    event_id: str
    sequence: int
    byte_start: int
    byte_end: int
    project_id: str | None
    record: dict
    normalized_hash: str


@dataclass(frozen=True)
class BatchCommit:
    access_domain: str
    batch_id: str
    payload_digest: str
    session_uid: str
    source_generation: str
    events: list[StoredEvent]
    rejected: list[dict]
    schema_version: str


@dataclass(frozen=True)
class SessionSource:
    access_domain: str
    session_uid: str
    source_generation: str
    provider: str
    origin: str
    retention: str


@dataclass(frozen=True)
class ArchiveEntry:
    access_domain: str
    session_uid: str
    source_generation: str
    byte_start: int
    byte_end: int
    object_digest: str


class Catalog:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        for statement in PRAGMAS + SCHEMA:
            self.db.execute(statement)

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def _transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def _scope(self, batch: BatchCommit) -> tuple[str, str, str]:
        return batch.access_domain, batch.session_uid, batch.source_generation

    def _count(self, domain: str, amount: int) -> None:
        self.db.execute(
            COUNT,
            (domain, DUPLICATES, amount),
        )

    def _settled(self, scope: tuple[str, str, str], sequence: int) -> tuple | None:
        found = self.db.execute(
            SETTLED,
            (*scope, sequence, *scope, sequence),
        ).fetchone()
        return tuple(found) if found else None

    def _insert_event(self, batch: BatchCommit, event: StoredEvent) -> bool:
        existing = self._settled(self._scope(batch), event.sequence)
        if existing == ("event", event.event_id, event.normalized_hash, event.project_id):
            return False
        if existing:
            raise Conflict("an event at this sequence was committed with different content")
        self.db.execute(
            INSERT_EVENT,
            (
                batch.access_domain,
                event.event_id,
                batch.session_uid,
                batch.source_generation,
                event.sequence,
                event.byte_start,
                event.byte_end,
                event.project_id,
                json.dumps(event.record),
                event.normalized_hash,
            ),
        )
        return True

    def _insert_quarantine(self, batch: BatchCommit, item: dict) -> None:
        existing = self._settled(self._scope(batch), item["sequence"])
        if existing == ("quarantine", item["event_id"], item["reason"], None):
            return
        if existing:
            raise Conflict("an event at this sequence was committed with different content")
        self.db.execute(
            INSERT_QUARANTINE,
            (*self._scope(batch), item["sequence"], item["event_id"], item["reason"]),
        )

    def watermark(self, domain: str, session_uid: str, source_generation: str) -> int:
        row = self.db.execute(
            WATERMARK,
            (domain, session_uid, source_generation),
        ).fetchone()
        return row[0] if row else 0

    def _advance(self, batch: BatchCommit) -> int:
        scope = self._scope(batch)
        current = self.watermark(*scope)
        through = self.db.execute(CONTIGUOUS, (*scope, *scope, current, current)).fetchone()[0]
        self.db.execute(ADVANCE, (*scope, through, batch.schema_version))
        return through

    def _replay(self, batch: BatchCommit, stored: tuple[str, str]) -> dict:
        if stored[0] != batch.payload_digest:
            raise Conflict("this batch_id was committed with different content")
        result = json.loads(stored[1])
        self._count(batch.access_domain, len(result["accepted"]["event_ids"]))
        return {**result, "state": "replayed", "durable_through": self.watermark(*self._scope(batch))}

    def _apply(self, batch: BatchCommit) -> dict:
        duplicates = [event.event_id for event in batch.events if not self._insert_event(batch, event)]
        for item in batch.rejected:
            self._insert_quarantine(batch, item)
        self._count(batch.access_domain, len(duplicates))
        accepted = sorted(batch.events, key=lambda event: event.sequence)
        result = {
            "accepted": {
                "first_sequence": accepted[0].sequence if accepted else None,
                "last_sequence": accepted[-1].sequence if accepted else None,
                "event_ids": [event.event_id for event in accepted],
            },
            "duplicates": duplicates,
            "rejected": batch.rejected,
        }
        self.db.execute(
            INSERT_BATCH,
            (
                batch.access_domain,
                batch.batch_id,
                batch.payload_digest,
                json.dumps(result),
                batch.schema_version,
            ),
        )
        return {**result, "state": "committed", "durable_through": self._advance(batch)}

    def commit(self, batch: BatchCommit) -> dict:
        with self._transaction():
            stored = self.db.execute(
                BATCH,
                (batch.access_domain, batch.batch_id),
            ).fetchone()
            return self._replay(batch, stored) if stored else self._apply(batch)

    def events(self, domain: str, session_uid: str, source_generation: str) -> list[dict]:
        rows = self.db.execute(
            EVENTS,
            (domain, session_uid, source_generation),
        ).fetchall()
        names = ("event_id", "sequence", "byte_start", "byte_end", "project_id", "record", "normalized_hash")
        return [{**dict(zip(names, row)), "record": json.loads(row[5])} for row in rows]

    def quarantined(self, domain: str, session_uid: str, source_generation: str) -> list[dict]:
        rows = self.db.execute(
            QUARANTINED,
            (domain, session_uid, source_generation),
        ).fetchall()
        return [{"sequence": row[0], "event_id": row[1], "reason": row[2]} for row in rows]

    def duplicate_events_total(self, domain: str) -> int:
        row = self.db.execute(COUNTER, (domain, DUPLICATES)).fetchone()
        return row[0] if row else 0

    def session(self, domain: str, session_uid: str) -> dict | None:
        row = self.db.execute(SESSION, (domain, session_uid)).fetchone()
        return dict(zip(("provider", "retention", "state", "revision"), row)) if row else None

    def register(self, source: SessionSource) -> dict:
        if source.retention not in RETENTIONS:
            raise ValueError(UNKNOWN_RETENTION)
        key = (source.access_domain, source.session_uid)
        with self._transaction():
            found = self.session(*key)
            if found is None:
                self.db.execute(INSERT_SESSION, (*key, source.provider, source.retention, SESSION_STATES[0]))
            elif (found["provider"], found["retention"]) != (source.provider, source.retention):
                raise Conflict(SESSION_CONFLICT)
            origin = self.db.execute(SOURCE, (*key, source.source_generation)).fetchone()
            if origin is None:
                self.db.execute(INSERT_SOURCE, (*key, source.source_generation, source.origin))
            elif origin[0] != source.origin:
                raise Conflict(SOURCE_CONFLICT)
            return self.session(*key)

    def sources(self, domain: str, session_uid: str) -> list[dict]:
        rows = self.db.execute(SOURCES, (domain, session_uid)).fetchall()
        return [{"source_generation": row[0], "origin": row[1]} for row in rows]

    def set_state(self, domain: str, session_uid: str, state: str, revision: int) -> int:
        if state not in SESSION_STATES:
            raise ValueError(UNKNOWN_STATE)
        with self._transaction():
            if self.db.execute(SET_STATE, (state, domain, session_uid, revision)).rowcount != 1:
                raise Conflict(STALE_REVISION)
            return self.session(domain, session_uid)["revision"]

    def archive(self, entry: ArchiveEntry) -> bool:
        scope = (entry.access_domain, entry.session_uid, entry.source_generation)
        with self._transaction():
            if self.db.execute(SOURCE, scope).fetchone() is None:
                raise Conflict(UNREGISTERED)
            found = self.db.execute(ENTRY, (*scope, entry.byte_start)).fetchone()
            if found is not None:
                if tuple(found) != (entry.byte_end, entry.object_digest):
                    raise Conflict(ARCHIVE_CONFLICT)
                return False
            if self.db.execute(OVERLAP, (*scope, entry.byte_end, entry.byte_start)).fetchone() is not None:
                raise Conflict(OVERLAP_CONFLICT)
            self.db.execute(INSERT_ENTRY, (*scope, entry.byte_start, entry.byte_end, entry.object_digest))
            return True

    def manifest(self, domain: str, session_uid: str, source_generation: str) -> list[dict]:
        rows = self.db.execute(MANIFEST, (domain, session_uid, source_generation)).fetchall()
        return [{"byte_start": row[0], "byte_end": row[1], "object_digest": row[2]} for row in rows]

    def integrity_failure(self, domain: str) -> None:
        self.db.execute(COUNT, (domain, ARCHIVE_FAILURES, 1))

    def integrity_failures_total(self, domain: str) -> int:
        row = self.db.execute(COUNTER, (domain, ARCHIVE_FAILURES)).fetchone()
        return row[0] if row else 0

    def snapshot(self) -> dict[str, list]:
        return {table: sorted(self.db.execute(f"SELECT * FROM {table}").fetchall()) for table in TABLES}
