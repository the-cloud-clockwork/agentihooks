import json
import math
import time
from dataclasses import dataclass

from hooks.memory import archive
from hooks.memory.api.ingest import Grant
from hooks.memory.catalog import Catalog, Conflict

STAGES = ("local_spool", "archive", "indexed_chunk", "brain_consumed")
UPSTREAM = {"archive": "local_spool", "indexed_chunk": "archive", "brain_consumed": "archive"}
NAMES = {"archive": "archive", "indexed_chunk": "embedding", "brain_consumed": "extraction"}
LAG_METRICS = tuple(f"{name}_lag_seconds" for name in NAMES.values())
SCHEMA = """CREATE TABLE IF NOT EXISTS memory_stage_progress (
    access_domain TEXT NOT NULL, session_uid TEXT NOT NULL, source_generation TEXT NOT NULL,
    stage TEXT NOT NULL, through INTEGER NOT NULL, recorded_at REAL NOT NULL,
    PRIMARY KEY (access_domain, session_uid, source_generation, stage, through))"""
KEY = "access_domain = ? AND session_uid = ? AND source_generation = ?"
POINTS = f"SELECT stage, through, recorded_at FROM memory_stage_progress WHERE {KEY} ORDER BY through"
INSERT = "INSERT INTO memory_stage_progress VALUES (?, ?, ?, ?, ?, ?)"
BINDINGS = """CREATE TABLE IF NOT EXISTS memory_progress_source (
    access_domain TEXT NOT NULL, session_uid TEXT NOT NULL, source_generation TEXT NOT NULL,
    project_id TEXT NOT NULL, PRIMARY KEY (access_domain, session_uid, source_generation))"""
CAPTURES = """CREATE TABLE IF NOT EXISTS memory_progress_batch (
    access_domain TEXT NOT NULL, session_uid TEXT NOT NULL, source_generation TEXT NOT NULL,
    identity TEXT NOT NULL, through INTEGER NOT NULL,
    PRIMARY KEY (access_domain, session_uid, source_generation, identity))"""
SOURCES = """SELECT s.session_uid, s.source_generation FROM (
    SELECT access_domain, session_uid, source_generation FROM session_source UNION
    SELECT access_domain, session_uid, source_generation FROM memory_progress_source) s
    LEFT JOIN session_record r USING (access_domain, session_uid)
    LEFT JOIN memory_progress_source b USING (access_domain, session_uid, source_generation)
    WHERE s.access_domain = ? AND (r.state IS NULL OR r.state = 'retained')
    AND (r.session_uid IS NOT NULL OR NOT EXISTS (SELECT 1 FROM session_tombstone t
        WHERE t.access_domain = s.access_domain AND t.session_uid = s.session_uid))
    AND (b.project_id IN (SELECT value FROM json_each(?)) OR EXISTS (
        SELECT 1 FROM session_event e WHERE e.access_domain = s.access_domain
        AND e.session_uid = s.session_uid AND e.project_id IN (SELECT value FROM json_each(?))))
    AND NOT EXISTS (SELECT 1 FROM session_event e WHERE e.access_domain = s.access_domain
        AND e.session_uid = s.session_uid AND e.project_id IS NOT NULL
        AND e.project_id NOT IN (SELECT value FROM json_each(?)))
    ORDER BY s.session_uid, s.source_generation"""


@dataclass(frozen=True)
class Scope:
    access_domain: str
    session_uid: str
    source_generation: str

    def key(self) -> tuple[str, str, str]:
        return self.access_domain, self.session_uid, self.source_generation


def _clock(now: float) -> None:
    if not math.isfinite(now) or now < 0:
        raise ValueError("invalid observation clock")


def _stage(points: dict, stage: str, now: float, stale_after: float) -> dict:
    own = points.get(stage, [])
    upstream = points.get(UPSTREAM.get(stage, stage), [])
    through = own[-1][0] if own else None
    pending = [(end, stamp) for end, stamp in upstream if end > (through or 0)]
    backlog = upstream[-1][0] - (through or 0) if upstream else None
    lag = max(0.0, now - min(stamp for _, stamp in pending)) if pending else 0.0 if own and upstream else None
    state = (
        "unknown"
        if not own or not upstream
        else "stale"
        if lag >= stale_after and backlog
        else "lagging"
        if backlog
        else "current"
    )
    return {
        "through": through,
        "updated_at": own[-1][1] if own else None,
        "age_seconds": max(0.0, now - own[-1][1]) if own else None,
        "backlog": backlog,
        "lag_seconds": lag,
        "state": state,
    }


class Metrics:
    """Trusted stage producers report contiguous normalized record sequences after their own durable acknowledgement."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        for statement in (SCHEMA, BINDINGS, CAPTURES):
            catalog.db.execute(statement)

    def _authorize(self, grant: Grant, scope: Scope) -> None:
        if scope.access_domain != grant.corpus:
            raise archive.EvidenceRefused(archive.FORBIDDEN)
        allowed = json.dumps(sorted(grant.projects))
        rows = self.catalog.db.execute(SOURCES, (grant.corpus, allowed, allowed, allowed))
        if (scope.session_uid, scope.source_generation) not in rows.fetchall():
            raise archive.EvidenceRefused(archive.UNAVAILABLE)

    def bind(self, grant: Grant, scope: Scope, project: str) -> None:
        if scope.access_domain != grant.corpus or project not in grant.projects:
            raise archive.EvidenceRefused(archive.FORBIDDEN)
        found = self.catalog.session(scope.access_domain, scope.session_uid)
        if found is not None and found["state"] != "retained":
            raise archive.EvidenceRefused(archive.UNAVAILABLE)
        allowed = json.dumps(sorted(grant.projects))
        foreign = self.catalog.db.execute(
            "SELECT 1 FROM session_event WHERE access_domain = ? AND session_uid = ? "
            "AND project_id IS NOT NULL AND project_id NOT IN (SELECT value FROM json_each(?)) LIMIT 1",
            (scope.access_domain, scope.session_uid, allowed),
        ).fetchone()
        if foreign:
            raise archive.EvidenceRefused(archive.UNAVAILABLE)
        with self.catalog._transaction():
            existing = self.catalog.db.execute(
                f"SELECT project_id FROM memory_progress_source WHERE {KEY}", scope.key()
            ).fetchone()
            if existing is not None and existing[0] != project:
                raise Conflict("source progress scope is immutable")
            self.catalog.db.execute(
                "INSERT OR IGNORE INTO memory_progress_source VALUES (?, ?, ?, ?)", (*scope.key(), project)
            )

    def _points(self, scope: Scope) -> dict:
        found: dict[str, list] = {}
        for stage, through, stamp in self.catalog.db.execute(POINTS, scope.key()):
            found.setdefault(stage, []).append((through, stamp))
        return found

    def advance(self, grant: Grant, scope: Scope, stage: str, through: int, now: float) -> bool:
        self._authorize(grant, scope)
        with self.catalog._transaction():
            return self._advance(scope, stage, through, now)

    def _advance(self, scope: Scope, stage: str, through: int, now: float) -> bool:
        _clock(now)
        if stage not in STAGES or type(through) is not int or not 0 <= through <= 9223372036854775807:
            raise ValueError("invalid stage watermark")
        points = self._points(scope)
        own = points.get(stage, [])
        if own and (through < own[-1][0] or now < own[-1][1]):
            raise Conflict("stale stage observation")
        if own and through == own[-1][0]:
            return False
        if stage in UPSTREAM:
            upstream = points.get(UPSTREAM[stage], [])
            if not upstream or through > upstream[-1][0]:
                raise Conflict("stage acknowledgement exceeds observed upstream")
        self.catalog.db.execute(INSERT, (*scope.key(), stage, through, now))
        return True

    def _status(self, scope: Scope, now: float, stale_after: float = 300.0) -> dict:
        _clock(now)
        if not math.isfinite(stale_after) or stale_after <= 0:
            raise ValueError("stale threshold must be positive and finite")
        points = self._points(scope)
        stages = {stage: _stage(points, stage, now, stale_after) for stage in STAGES}
        complete = all(
            value["through"] == stages["local_spool"]["through"] and value["state"] == "current"
            for value in stages.values()
        )
        alerts = []
        for stage, name in NAMES.items():
            value = stages[stage]
            if value["state"] == "unknown":
                alerts.append(f"{name}_unknown")
            elif value["backlog"] and value["lag_seconds"] >= stale_after:
                alerts.append(f"{name}_stale")
        return {
            "session_uid": scope.session_uid,
            "source_generation": scope.source_generation,
            "coverage": "complete" if complete else "partial",
            "stages": stages,
            "alerts": alerts,
            **{f"{name}_lag_seconds": stages[stage]["lag_seconds"] for stage, name in NAMES.items()},
        }

    def status(self, grant: Grant, scope: Scope, now: float, stale_after: float = 300.0) -> dict:
        self._authorize(grant, scope)
        return self._status(scope, now, stale_after)

    def coverage(self, grant: Grant, projects: list[str], now: float | None = None) -> dict:
        if not set(projects) <= grant.projects:
            raise archive.EvidenceRefused(archive.FORBIDDEN)
        scoped = Grant(grant.principal, grant.corpus, frozenset(projects or grant.projects))
        allowed = json.dumps(sorted(scoped.projects))
        clock = time.time() if now is None else now
        sources = [
            self._status(Scope(scoped.corpus, *row), clock)
            for row in self.catalog.db.execute(SOURCES, (scoped.corpus, allowed, allowed, allowed)).fetchall()
        ]
        state = (
            "complete"
            if sources and all(source["coverage"] == "complete" for source in sources)
            else "partial"
            if sources
            else "unknown"
        )
        return {"state": state, "sources": sources}

    def snapshot(self) -> dict:
        return {
            name: sorted(self.catalog.db.execute(f"SELECT * FROM {name}").fetchall())
            for name in ("memory_stage_progress", "memory_progress_source", "memory_progress_batch")
        }
