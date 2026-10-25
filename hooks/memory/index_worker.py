"""Bounded incremental embedding: queue changed chunks, embed them in limited batches, keep progress through outages."""

import json
from dataclasses import dataclass

from hooks.memory import embeddings
from hooks.memory.api.ingest import Grant
from hooks.memory.catalog import Catalog, Conflict
from hooks.memory.chunking import ChunkIndex
from hooks.memory.embeddings import Embedder, EmbeddingModel, TransientEmbeddingError

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS index_generation (
        access_domain TEXT NOT NULL, index_generation TEXT NOT NULL, model TEXT NOT NULL, dimension INTEGER NOT NULL,
        paused INTEGER NOT NULL, PRIMARY KEY (access_domain, index_generation))""",
    """CREATE TABLE IF NOT EXISTS index_cutover (
        access_domain TEXT NOT NULL PRIMARY KEY, index_generation TEXT NOT NULL, revision INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS embedding_work (
        access_domain TEXT NOT NULL, index_generation TEXT NOT NULL, chunk_id TEXT NOT NULL, session_uid TEXT NOT NULL,
        chunker_version TEXT NOT NULL, content_hash TEXT NOT NULL, queued_at REAL NOT NULL, attempts INTEGER NOT NULL,
        last_error TEXT, PRIMARY KEY (access_domain, index_generation, chunk_id))""",
    """CREATE TABLE IF NOT EXISTS chunk_vector (
        access_domain TEXT NOT NULL, index_generation TEXT NOT NULL, chunk_id TEXT NOT NULL, session_uid TEXT NOT NULL,
        chunker_version TEXT NOT NULL, content_hash TEXT NOT NULL, model TEXT NOT NULL, dimension INTEGER NOT NULL,
        vector TEXT NOT NULL, PRIMARY KEY (access_domain, index_generation, chunk_id))""",
)
INDEX_TABLES = ("index_generation", "index_cutover", "embedding_work", "chunk_vector")
INDEX_KEY = "access_domain = ? AND index_generation = ?"
SESSION_KEY = f"{INDEX_KEY} AND session_uid = ? AND chunker_version = ?"
GENERATION = f"SELECT model, dimension, paused FROM index_generation WHERE {INDEX_KEY}"
INSERT_GENERATION = "INSERT INTO index_generation VALUES (?, ?, ?, ?, 0)"
SET_PAUSED = f"UPDATE index_generation SET paused = ? WHERE {INDEX_KEY}"
CUTOVER = "SELECT index_generation, revision FROM index_cutover WHERE access_domain = ?"
SAVE_CUTOVER = (
    "INSERT INTO index_cutover VALUES (?, ?, ?) ON CONFLICT (access_domain) "
    "DO UPDATE SET index_generation = excluded.index_generation, revision = excluded.revision"
)
WORK_HASHES = f"SELECT chunk_id, content_hash FROM embedding_work WHERE {SESSION_KEY}"
VECTOR_HASHES = f"SELECT chunk_id, content_hash FROM chunk_vector WHERE {SESSION_KEY}"
QUEUE = (
    "INSERT INTO embedding_work VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL) "
    "ON CONFLICT (access_domain, index_generation, chunk_id) DO UPDATE SET content_hash = excluded.content_hash, "
    "queued_at = excluded.queued_at, attempts = 0, last_error = NULL"
)
DROP_WORK = f"DELETE FROM embedding_work WHERE {INDEX_KEY} AND chunk_id = ?"
DROP_VECTOR = f"DELETE FROM chunk_vector WHERE {INDEX_KEY} AND chunk_id = ?"
MATCHING_CHUNK = (
    "SELECT 1 FROM search_chunk s WHERE s.access_domain = embedding_work.access_domain "
    "AND s.session_uid = embedding_work.session_uid AND s.chunker_version = embedding_work.chunker_version "
    "AND s.chunk_id = embedding_work.chunk_id AND json_extract(s.record, '$.content_hash') = embedding_work.content_hash"
)
PURGE = f"DELETE FROM embedding_work WHERE {INDEX_KEY} AND NOT EXISTS ({MATCHING_CHUNK})"
PENDING = (
    "SELECT s.record FROM embedding_work JOIN search_chunk s ON s.access_domain = embedding_work.access_domain "
    "AND s.session_uid = embedding_work.session_uid AND s.chunker_version = embedding_work.chunker_version "
    "AND s.chunk_id = embedding_work.chunk_id "
    "WHERE embedding_work.access_domain = ? AND embedding_work.index_generation = ? "
    "ORDER BY embedding_work.queued_at, embedding_work.chunk_id LIMIT ?"
)
UNCOVERED = (
    "SELECT COUNT(*) FROM search_chunk s WHERE s.access_domain = ? AND EXISTS (SELECT 1 FROM session_record r "
    "WHERE r.access_domain = s.access_domain AND r.session_uid = s.session_uid AND r.state = 'retained') "
    "AND (EXISTS (SELECT 1 FROM chunk_cutover c WHERE c.access_domain = s.access_domain "
    "AND c.session_uid = s.session_uid AND c.chunker_version = s.chunker_version) "
    "OR EXISTS (SELECT 1 FROM chunk_vector v "
    "WHERE v.access_domain = s.access_domain AND v.index_generation = ? AND v.session_uid = s.session_uid "
    "AND v.chunker_version = s.chunker_version)) AND NOT EXISTS (SELECT 1 FROM chunk_vector v "
    "WHERE v.access_domain = s.access_domain AND v.index_generation = ? AND v.chunk_id = s.chunk_id "
    "AND v.content_hash = json_extract(s.record, '$.content_hash'))"
)
DEFER = f"UPDATE embedding_work SET attempts = attempts + 1, last_error = ? WHERE {INDEX_KEY} AND chunk_id = ?"
TAKE = f"DELETE FROM embedding_work WHERE {INDEX_KEY} AND chunk_id = ? AND content_hash = ?"
SAVE_VECTOR = (
    "INSERT INTO chunk_vector VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT (access_domain, index_generation, chunk_id) DO UPDATE SET content_hash = excluded.content_hash, "
    "vector = excluded.vector"
)
COUNT_PENDING = f"SELECT COUNT(*), MIN(queued_at) FROM embedding_work WHERE {INDEX_KEY}"
COUNT_WORK = f"SELECT COUNT(*) FROM embedding_work WHERE {INDEX_KEY}"
VECTORS = (
    f"SELECT chunk_id, content_hash, model, dimension, vector FROM chunk_vector WHERE {SESSION_KEY} ORDER BY chunk_id"
)
BACKLOG = "embedding_backlog_age_seconds"
BAD_LIMITS = "embedding limits need a positive batch size, batch character bound and batches per drain"
GENERATION_CONFLICT = "this index generation is bound to another embedding model or dimension"
UNKNOWN_INDEX = "no index generation is registered under this name"
MODEL_MISMATCH = "the embedder's model differs from the model bound to this index generation; nothing was written"
INCOMPLETE = (
    "only an index generation with no pending work and every searchable or tracked session embedded can become active"
)
STALE_CUTOVER = "the index cutover revision is stale; nothing changed"


@dataclass(frozen=True)
class Limits:
    batch_size: int
    batch_chars: int
    max_batches: int


DEFAULT_LIMITS = Limits(32, 64000, 8)


@dataclass(frozen=True)
class Target:
    access_domain: str
    session_uid: str
    chunker_version: str
    index_generation: str

    def key(self) -> tuple[str, str, str, str]:
        return self.access_domain, self.index_generation, self.session_uid, self.chunker_version


class IndexWorker:
    def __init__(self, catalog: Catalog, limits: Limits = DEFAULT_LIMITS) -> None:
        if min(limits.batch_size, limits.batch_chars, limits.max_batches) < 1:
            raise ValueError(BAD_LIMITS)
        self.catalog, self.limits, self.chunks = catalog, limits, ChunkIndex(catalog)
        for statement in SCHEMA:
            catalog.db.execute(statement)

    def _generation(self, domain: str, generation: str) -> tuple[EmbeddingModel, bool]:
        row = self.catalog.db.execute(GENERATION, (domain, generation)).fetchone()
        if row is None:
            raise Conflict(UNKNOWN_INDEX)
        return EmbeddingModel(row[0], row[1]), bool(row[2])

    def register(self, domain: str, generation: str, model: EmbeddingModel) -> dict:
        embeddings.require(model)
        with self.catalog._transaction():
            row = self.catalog.db.execute(GENERATION, (domain, generation)).fetchone()
            if row is None:
                self.catalog.db.execute(INSERT_GENERATION, (domain, generation, model.model, model.dimension))
            elif EmbeddingModel(row[0], row[1]) != model:
                raise Conflict(GENERATION_CONFLICT)
        return {"index_generation": generation, "model": model.model, "dimension": model.dimension}

    def set_paused(self, domain: str, generation: str, paused: bool) -> None:
        self._generation(domain, generation)
        self.catalog.db.execute(SET_PAUSED, (int(paused), domain, generation))

    def _hashes(self, query: str, target: Target) -> dict[str, str]:
        return dict(self.catalog.db.execute(query, target.key()).fetchall())

    def enqueue(self, grant: Grant, target: Target, now: float) -> dict:
        index = (target.access_domain, target.index_generation)
        with self.catalog._transaction():
            chunks = self.chunks.chunks(grant, target.access_domain, target.session_uid, target.chunker_version)
            current = {chunk["chunk_id"]: chunk["content_hash"] for chunk in chunks}
            self._generation(*index)
            done, queued = self._hashes(VECTOR_HASHES, target), self._hashes(WORK_HASHES, target)
            dropped = sorted((done.keys() | queued.keys()) - current.keys())
            for chunk_id in dropped:
                self.catalog.db.execute(DROP_WORK, (*index, chunk_id))
                self.catalog.db.execute(DROP_VECTOR, (*index, chunk_id))
            fresh = [key for key, value in current.items() if value not in (done.get(key), queued.get(key))]
            for chunk_id in fresh:
                row = (*index, chunk_id, target.session_uid, target.chunker_version, current[chunk_id], now)
                self.catalog.db.execute(QUEUE, row)
        embedded = sum(done.get(key) == value for key, value in current.items())
        return {"queued": fresh, "embedded": embedded, "dropped": dropped}

    def _batch(self, domain: str, generation: str) -> list[dict]:
        self.catalog.db.execute(PURGE, (domain, generation))
        rows = self.catalog.db.execute(PENDING, (domain, generation, self.limits.batch_size)).fetchall()
        batch, chars = [], 0
        for (stored,) in rows:
            record = json.loads(stored)
            if batch and chars + len(record["preview"]) > self.limits.batch_chars:
                break
            chars += len(record["preview"])
            batch.append(record)
        return batch

    def _commit(self, index: tuple[str, str], model: EmbeddingModel, batch: list[dict], vectors: list) -> list[str]:
        written = []
        with self.catalog._transaction():
            for chunk, vector in zip(batch, vectors):
                taken = (*index, chunk["chunk_id"], chunk["content_hash"])
                if self.catalog.db.execute(TAKE, taken).rowcount != 1:
                    continue
                identity = (chunk["session_uid"], chunk["chunker_version"], chunk["content_hash"])
                values = (*index, chunk["chunk_id"], *identity, model.model, model.dimension, json.dumps(vector))
                self.catalog.db.execute(SAVE_VECTOR, values)
                written.append(chunk["chunk_id"])
        return written

    def _defer(self, index: tuple[str, str], batch: list[dict], error: Exception) -> list[str]:
        failed = [chunk["chunk_id"] for chunk in batch]
        with self.catalog._transaction():
            for chunk_id in failed:
                self.catalog.db.execute(DEFER, (type(error).__name__, *index, chunk_id))
        return failed

    def drain(self, embedder: Embedder, domain: str, generation: str, now: float) -> dict:
        model, paused = self._generation(domain, generation)
        if embedder.model != model:
            raise Conflict(MODEL_MISMATCH)
        report = {"state": "paused", "embedded": [], "failed": []}
        for _ in range(0 if paused else self.limits.max_batches):
            batch = self._batch(domain, generation)
            if not batch:
                break
            texts = [chunk["preview"] for chunk in batch]
            try:
                vectors = embedder.embed(texts)
            except TransientEmbeddingError as error:
                report["failed"] = self._defer((domain, generation), batch, error)
                break
            vectors = embeddings.checked(model, texts, vectors)
            report["embedded"] += self._commit((domain, generation), model, batch, vectors)
        pending = self.pending(domain, generation, now)
        if not paused:
            report["state"] = "deferred" if report["failed"] else ("bounded" if pending["pending"] else "drained")
        return {**report, **pending}

    def pending(self, domain: str, generation: str, now: float) -> dict:
        count, oldest = self.catalog.db.execute(COUNT_PENDING, (domain, generation)).fetchone()
        return {"pending": count, BACKLOG: max(0.0, now - oldest) if count else 0.0}

    def coverage(self, grant: Grant, target: Target) -> dict:
        chunks = self.chunks.chunks(grant, target.access_domain, target.session_uid, target.chunker_version)
        done = self._hashes(VECTOR_HASHES, target)
        embedded = sum(done.get(chunk["chunk_id"]) == chunk["content_hash"] for chunk in chunks)
        state = "indexed" if embedded == len(chunks) else "incomplete"
        return {"state": state, "chunks": len(chunks), "embedded": embedded}

    def vectors(self, grant: Grant, target: Target) -> list[dict]:
        self.chunks.chunks(grant, target.access_domain, target.session_uid, target.chunker_version)
        rows = self.catalog.db.execute(VECTORS, target.key()).fetchall()
        names = ("chunk_id", "content_hash", "model", "dimension")
        return [{**dict(zip(names, row)), "vector": json.loads(row[4])} for row in rows]

    def active(self, domain: str) -> dict:
        row = self.catalog.db.execute(CUTOVER, (domain,)).fetchone()
        return {"index_generation": row[0], "revision": row[1]} if row else {"index_generation": None, "revision": 0}

    def activate(self, domain: str, generation: str, revision: int) -> int:
        with self.catalog._transaction():
            self._generation(domain, generation)
            if (
                self.catalog.db.execute(COUNT_WORK, (domain, generation)).fetchone()[0]
                or self.catalog.db.execute(UNCOVERED, (domain, generation, generation)).fetchone()[0]
            ):
                raise Conflict(INCOMPLETE)
            if self.active(domain)["revision"] != revision:
                raise Conflict(STALE_CUTOVER)
            self.catalog.db.execute(SAVE_CUTOVER, (domain, generation, revision + 1))
            return revision + 1

    def snapshot(self) -> dict[str, list]:
        return {table: sorted(self.catalog.db.execute(f"SELECT * FROM {table}").fetchall()) for table in INDEX_TABLES}
