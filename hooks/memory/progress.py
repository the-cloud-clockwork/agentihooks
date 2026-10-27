import hashlib
import json
import time
from collections.abc import Callable

from hooks.memory.api.ingest import Grant
from hooks.memory.catalog import Catalog, Conflict
from hooks.memory.metrics import KEY, STAGES, Metrics, Scope
from hooks.memory.spool import Entry

CAPTURED = f"SELECT through FROM memory_progress_batch WHERE {KEY} AND identity = ?"
BOUND = """SELECT b.session_uid, b.source_generation FROM memory_progress_source b
    JOIN session_record r USING (access_domain, session_uid)
    WHERE b.access_domain = ? AND r.state = 'retained'"""
COMPLETE = """SELECT DISTINCT g.built_through FROM search_chunk c
    JOIN chunk_generation g USING (access_domain, session_uid, chunker_version)
    WHERE c.access_domain = ? AND c.session_uid = ? AND NOT EXISTS (
        SELECT 1 FROM search_chunk missing WHERE missing.access_domain = c.access_domain
        AND missing.session_uid = c.session_uid AND missing.chunker_version = c.chunker_version
        AND NOT EXISTS (SELECT 1 FROM chunk_vector v WHERE v.access_domain = missing.access_domain
            AND v.chunk_id = missing.chunk_id AND v.index_generation = ?
            AND v.content_hash = json_extract(missing.record, '$.content_hash')))"""


class Recorder:
    def __init__(
        self, catalog: Catalog, grant: Grant, scope: Scope, project: str, clock: Callable[[], float] = time.time
    ) -> None:
        self.meter, self.grant, self.scope, self.clock = Metrics(catalog), grant, scope, clock
        self.meter.bind(grant, scope, project)

    def buffered(self, entry: Entry) -> None:
        self.meter._authorize(self.grant, self.scope)
        identity = hashlib.sha256(entry.identity.encode()).hexdigest()
        with self.meter.catalog._transaction():
            key = (*self.scope.key(), identity)
            if self.meter.catalog.db.execute(CAPTURED, key).fetchone() is not None:
                return
            points = self.meter._points(self.scope)
            previous = points["local_spool"][-1][0] if points else self.meter.catalog.watermark(*self.scope.key())
            through = previous + len(entry.records)
            self.meter._advance(self.scope, "local_spool", through, entry.created)
            if not points:
                for stage in STAGES[1:]:
                    self.meter._advance(self.scope, stage, 0, 0.0)
            self.meter.catalog.db.execute("INSERT INTO memory_progress_batch VALUES (?, ?, ?, ?, ?)", (*key, through))

    def archived(self, entry: Entry) -> None:
        identity = hashlib.sha256(entry.identity.encode()).hexdigest()
        row = self.meter.catalog.db.execute(CAPTURED, (*self.scope.key(), identity)).fetchone()
        if row is None:
            raise Conflict("archive acknowledgement has no captured batch")
        self.meter.advance(self.grant, self.scope, "archive", row[0], self.clock())

    def consume(self, through: int, commit: Callable[[Scope, int], dict]) -> dict:
        """The extractor returns a committed cursor only after its synthesized output is durable."""
        self.meter._authorize(self.grant, self.scope)
        points = self.meter._points(self.scope)
        current = points.get("brain_consumed", [(0, 0.0)])[-1][0]
        if type(through) is not int or through < current or through > points.get("archive", [(0, 0.0)])[-1][0]:
            raise Conflict("extraction cursor is stale or exceeds archive")
        if through == current:
            return {"state": "replayed", "through": through}
        receipt = commit(self.scope, through)
        if (
            receipt.get("state") != "committed"
            or type(receipt.get("through")) is not int
            or receipt["through"] != through
        ):
            raise Conflict("extraction did not acknowledge the requested durable cursor")
        self.meter.advance(self.grant, self.scope, "brain_consumed", through, self.clock())
        return receipt


def indexed(catalog: Catalog, domain: str, generation: str, now: float) -> None:
    meter = Metrics(catalog)
    with catalog._transaction():
        for uid, source in catalog.db.execute(BOUND, (domain,)).fetchall():
            covered = catalog.db.execute(COMPLETE, (domain, uid, generation)).fetchall()
            through = max((json.loads(row[0]).get(source, 0) for row in covered), default=0)
            scope = Scope(domain, uid, source)
            points = meter._points(scope)
            current = points.get("indexed_chunk", [(0, 0.0)])[-1][0]
            archived = points.get("archive", [(0, 0.0)])[-1][0]
            if current < through <= archived:
                meter._advance(scope, "indexed_chunk", through, now)
