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
)
TABLES = ("session_event", "session_quarantine", "session_ingest_batch", "session_watermark", "ingest_counter")
DUPLICATES = "ingest_duplicate_events_total"
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

    def snapshot(self) -> dict[str, list]:
        return {table: sorted(self.db.execute(f"SELECT * FROM {table}").fetchall()) for table in TABLES}
