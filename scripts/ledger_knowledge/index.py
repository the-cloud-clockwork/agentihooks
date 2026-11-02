"""Per ledger knowledge index in SQLite: chunks embedded incrementally and queried by vector similarity."""

import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import astuple, dataclass
from pathlib import Path
from typing import Protocol

from .chunks import Chunk

DIMENSION = 512
SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    key TEXT PRIMARY KEY,
    ref TEXT NOT NULL,
    parent TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    text TEXT NOT NULL,
    digest TEXT NOT NULL,
    vector TEXT NOT NULL,
    changed_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
"""
COLUMNS = "key, ref, parent, kind, title, text"


class Embedder(Protocol):
    """Turns texts into vectors of one dimension, one vector per text in order."""

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashEmbedder:
    """Deterministic local embedder: hashed word counts, unit length."""

    def __init__(self, dimension: int = DIMENSION) -> None:
        self.dimension = dimension

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            vector[int(hashlib.sha256(word.encode()).hexdigest()[:8], 16) % self.dimension] += 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector] if norm else vector

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]


@dataclass(frozen=True)
class Refresh:
    """Counts of one refresh: only added and changed chunks were embedded."""

    added: int = 0
    changed: int = 0
    removed: int = 0
    unchanged: int = 0


@dataclass(frozen=True)
class Hit:
    """A matching chunk with its parents, nearest first, up to the ledger root."""

    chunk: Chunk
    score: float
    parents: list[Chunk]


def digest(chunk: Chunk) -> str:
    """Content digest of a chunk; a changed digest means the chunk is embedded again."""
    return hashlib.sha256(json.dumps(astuple(chunk)).encode()).hexdigest()


class KnowledgeIndex:
    """One ledger's chunks and vectors in a SQLite file."""

    def __init__(self, path: Path, embedder: Embedder) -> None:
        self.path = path
        self.embedder = embedder
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def refresh(self, chunks: list[Chunk], now: int) -> Refresh:
        """Embed new and changed chunks, drop vanished ones and stamp the refresh time."""
        with self._connect() as connection:
            known = dict(connection.execute("SELECT key, digest FROM chunks"))
            fresh = [(chunk, digest(chunk)) for chunk in chunks if known.get(chunk.key) != digest(chunk)]
            vectors = self.embedder.embed([chunk.text for chunk, _ in fresh]) if fresh else []
            for (chunk, sha), vector in zip(fresh, vectors, strict=True):
                connection.execute(
                    f"INSERT OR REPLACE INTO chunks ({COLUMNS}, digest, vector, changed_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*astuple(chunk), sha, json.dumps(vector), now),
                )
            gone = known.keys() - {chunk.key for chunk in chunks}
            connection.executemany("DELETE FROM chunks WHERE key = ?", [(key,) for key in gone])
            connection.execute("INSERT OR REPLACE INTO meta (name, value) VALUES ('refreshed_at', ?)", (now,))
        added = sum(chunk.key not in known for chunk, _ in fresh)
        return Refresh(added, len(fresh) - added, len(gone), len(chunks) - len(fresh))

    def refreshed_at(self) -> int:
        """Epoch milliseconds of the last refresh, zero before the first."""
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM meta WHERE name = 'refreshed_at'").fetchone()
        return row[0] if row else 0

    def _chunk(self, connection: sqlite3.Connection, key: str) -> Chunk | None:
        row = connection.execute(f"SELECT {COLUMNS} FROM chunks WHERE key = ?", (key,)).fetchone()
        return Chunk(*row) if row else None

    def get(self, key: str) -> Chunk | None:
        """One chunk by key, or None."""
        with self._connect() as connection:
            return self._chunk(connection, key)

    def _parents(self, connection: sqlite3.Connection, chunk: Chunk) -> list[Chunk]:
        parents: list[Chunk] = []
        parent = self._chunk(connection, f"{chunk.parent}#0") if chunk.parent else None
        while parent is not None and parent not in parents:
            parents.append(parent)
            parent = self._chunk(connection, f"{parent.parent}#0") if parent.parent else None
        return parents

    def query(self, question: str, limit: int) -> list[Hit]:
        """The chunks most similar to the question, each with its parent chain."""
        (target,) = self.embedder.embed([question])
        with self._connect() as connection:
            scored = sorted(
                (
                    (-sum(a * b for a, b in zip(target, json.loads(vector), strict=True)), Chunk(*row))
                    for *row, vector in connection.execute(f"SELECT {COLUMNS}, vector FROM chunks")
                ),
                key=lambda pair: (pair[0], pair[1].key),
            )
            return [
                Hit(chunk, -score, self._parents(connection, chunk)) for score, chunk in scored[:limit] if score < 0
            ]

    def changes(self, limit: int) -> list[tuple[int, Chunk]]:
        """The most recently added or changed chunks with their change time, newest first."""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT changed_at, {COLUMNS} FROM chunks ORDER BY changed_at DESC, key LIMIT ?", (limit,)
            ).fetchall()
        return [(at, Chunk(*row)) for at, *row in rows]

    def counts(self) -> dict[str, int]:
        """Items per kind, each item counted once whatever its chunk count."""
        with self._connect() as connection:
            return dict(connection.execute("SELECT kind, count(DISTINCT ref) FROM chunks GROUP BY kind ORDER BY kind"))
