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
SOURCES = """SELECT s.session_uid, s.source_generation FROM session_source s
    JOIN session_record r USING (access_domain, session_uid)
    WHERE s.access_domain = ? AND r.state = 'retained'
    AND EXISTS (SELECT 1 FROM session_event e WHERE e.access_domain = s.access_domain
        AND e.session_uid = s.session_uid AND e.project_id IN (SELECT value FROM json_each(?)))
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
        catalog.db.execute(SCHEMA)

    def _authorize(self, grant: Grant, scope: Scope) -> None:
        _, sources = archive._readable(self.catalog, grant, scope.access_domain, scope.session_uid)
        if not any(source["source_generation"] == scope.source_generation for source in sources):
            raise archive.EvidenceRefused(archive.UNAVAILABLE)
        if not any(event["project_id"] in grant.projects for source in sources for event in source["events"]):
            raise archive.EvidenceRefused(archive.UNAVAILABLE)

    def _points(self, scope: Scope) -> dict:
        found: dict[str, list] = {}
        for stage, through, stamp in self.catalog.db.execute(POINTS, scope.key()):
            found.setdefault(stage, []).append((through, stamp))
        return found

    def advance(self, grant: Grant, scope: Scope, stage: str, through: int, now: float) -> bool:
        self._authorize(grant, scope)
        _clock(now)
        if stage not in STAGES or type(through) is not int or not 0 <= through <= 9223372036854775807:
            raise ValueError("invalid stage watermark")
        with self.catalog._transaction():
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

    def status(self, grant: Grant, scope: Scope, now: float, stale_after: float = 300.0) -> dict:
        self._authorize(grant, scope)
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

    def coverage(self, grant: Grant, projects: list[str], now: float | None = None) -> dict:
        if not set(projects) <= grant.projects:
            raise archive.EvidenceRefused(archive.FORBIDDEN)
        scoped = Grant(grant.principal, grant.corpus, frozenset(projects or grant.projects))
        allowed = json.dumps(sorted(scoped.projects))
        clock = time.time() if now is None else now
        sources = [
            self.status(scoped, Scope(scoped.corpus, *row), clock)
            for row in self.catalog.db.execute(SOURCES, (scoped.corpus, allowed, allowed)).fetchall()
        ]
        state = (
            "complete"
            if sources and all(source["coverage"] == "complete" for source in sources)
            else "partial"
            if sources
            else "unknown"
        )
        return {"state": state, "sources": sources}

    def snapshot(self) -> list:
        return self.catalog.db.execute("SELECT * FROM memory_stage_progress ORDER BY 1, 2, 3, 4, 5").fetchall()
