import hashlib
import json
import os
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .models import RecallRecord

FIELDS = ("ledger_slug", "swarm_slug", "kind", "ref", "parent_ref", "author", "time", "title", "text", "chunk_index")

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    {", ".join(FIELDS)},
    content_hash TEXT NOT NULL,
    source_state TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS records_source_ref ON records(source, ref);
CREATE VIRTUAL TABLE IF NOT EXISTS recall_fts USING fts5(title, text, tokenize="unicode61 tokenchars '-_'");
"""


@dataclass(frozen=True)
class SyncCounts:
    written: int = 0
    unchanged: int = 0
    archived: int = 0
    removed: int = 0


@runtime_checkable
class RecallStore(Protocol):
    def sync(self, source: str, records: Iterable[RecallRecord]) -> SyncCounts: ...

    def remove(self, source: str, refs: Iterable[str]) -> int: ...

    def match(self, expression: str) -> list[str]: ...


def agentihooks_home(environ: Mapping[str, str] = os.environ) -> Path:
    return Path(environ.get("AGENTIHOOKS_HOME") or Path.home() / ".agentihooks")


def default_path(environ: Mapping[str, str] = os.environ) -> Path:
    return agentihooks_home(environ) / "recall" / "recall.sqlite3"


def content_hash(record: RecallRecord) -> str:
    return hashlib.sha256(json.dumps(asdict(record)).encode()).hexdigest()


class SQLiteRecallStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")

    def _drop(self, connection: sqlite3.Connection, rowid: int) -> None:
        connection.execute("DELETE FROM records WHERE id = ?", (rowid,))
        connection.execute("DELETE FROM recall_fts WHERE rowid = ?", (rowid,))

    def _put(self, connection: sqlite3.Connection, source: str, record: RecallRecord, digest: str, rowid) -> None:
        if rowid is not None:
            self._drop(connection, rowid)
        values = [getattr(record, field) for field in FIELDS]
        cursor = connection.execute(
            f"INSERT INTO records (id, key, source, {', '.join(FIELDS)}, content_hash, source_state)"
            f" VALUES (?, ?, ?, {', '.join('?' for _ in FIELDS)}, ?, 'present')",
            (rowid, record.key, source, *values, digest),
        )
        connection.execute(
            "INSERT INTO recall_fts (rowid, title, text) VALUES (?, ?, ?)",
            (cursor.lastrowid, record.title, record.text),
        )

    def sync(self, source: str, records: Iterable[RecallRecord]) -> SyncCounts:
        records = list(records)
        refs = {record.ref for record in records}
        counts = dict.fromkeys(("written", "unchanged", "archived", "removed"), 0)
        with self._write() as connection:
            existing = {
                key: (rowid, ref, digest, state)
                for rowid, key, ref, digest, state in connection.execute(
                    "SELECT id, key, ref, content_hash, source_state FROM records WHERE source = ?", (source,)
                )
            }
            for record in records:
                digest = content_hash(record)
                rowid, _, old_digest, state = existing.pop(record.key, (None, "", "", ""))
                if (old_digest, state) == (digest, "present"):
                    counts["unchanged"] += 1
                else:
                    self._put(connection, source, record, digest, rowid)
                    counts["written"] += 1
            for rowid, ref, _, state in existing.values():
                if ref in refs:
                    self._drop(connection, rowid)
                    counts["removed"] += 1
                elif state == "present":
                    connection.execute("UPDATE records SET source_state = 'archived' WHERE id = ?", (rowid,))
                    counts["archived"] += 1
        return SyncCounts(**counts)

    def remove(self, source: str, refs: Iterable[str]) -> int:
        removed = 0
        with self._write() as connection:
            for ref in refs:
                rows = connection.execute(
                    "SELECT id FROM records WHERE source = ? AND (ref = ? OR substr(ref, 1, ?) = ?)",
                    (source, ref, len(ref) + 1, f"{ref}/"),
                ).fetchall()
                for (rowid,) in rows:
                    self._drop(connection, rowid)
                removed += len(rows)
        return removed

    def match(self, expression: str) -> list[str]:
        with self.connect() as connection:
            return [
                key
                for (key,) in connection.execute(
                    "SELECT records.key FROM recall_fts JOIN records ON records.id = recall_fts.rowid"
                    " WHERE recall_fts MATCH ? ORDER BY records.key",
                    (expression,),
                )
            ]
