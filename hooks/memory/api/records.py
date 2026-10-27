"""Session tools: list, search, get and record range expansion, each authorized by the caller's grant.

A search hit carries a citation that expands to the exact catalog records and archived bytes it was built from;
a session that is no longer retained or a range the catalog or archive no longer holds is refused, never shown.
Pagination cursors are catalog rows bound to the grant and the request: a restart keeps them, and an elapsed
lifetime or a changed listing expires them explicitly instead of returning a mismatched range.
"""

import json
import sqlite3
from datetime import datetime

from hooks.memory import archive, metrics
from hooks.memory import search as core
from hooks.memory.api import search as search_api
from hooks.memory.api.ingest import SCHEMA_VERSION, Grant, Rejected, digest
from hooks.memory.catalog import COUNT, COUNTER, Catalog
from hooks.memory.embeddings import Embedder

SCHEMA = """CREATE TABLE IF NOT EXISTS session_cursor (
    access_domain TEXT NOT NULL, cursor_id TEXT NOT NULL, grant_digest TEXT NOT NULL, request_digest TEXT NOT NULL,
    position INTEGER NOT NULL, fingerprint TEXT NOT NULL, expires_at REAL NOT NULL,
    PRIMARY KEY (access_domain, cursor_id))"""
VISIBLE = (
    "SELECT r.session_uid, r.provider FROM session_record r WHERE r.access_domain = ? AND r.state = 'retained' "
    "AND (? IS NULL OR r.session_uid = ?) AND NOT EXISTS (SELECT 1 FROM session_event e "
    "WHERE e.access_domain = r.access_domain AND e.session_uid = r.session_uid AND e.project_id IS NOT NULL "
    "AND e.project_id NOT IN (SELECT value FROM json_each(?))) AND EXISTS (SELECT 1 FROM session_event e "
    "WHERE e.access_domain = r.access_domain AND e.session_uid = r.session_uid "
    "AND e.project_id IN (SELECT value FROM json_each(?))) ORDER BY r.session_uid"
)
CHUNK = "SELECT record FROM search_chunk WHERE access_domain = ? AND session_uid = ? AND chunk_id = ?"
ISSUE = "INSERT OR IGNORE INTO session_cursor VALUES (?, ?, ?, ?, ?, ?, ?)"
PRUNE = "DELETE FROM session_cursor WHERE access_domain = ? AND expires_at <= ?"
CURSOR = (
    "SELECT grant_digest, request_digest, position, fingerprint, expires_at FROM session_cursor "
    "WHERE access_domain = ? AND cursor_id = ?"
)
COMMON_FIELDS = frozenset({"schema_version", "access_domain"})
LIST_FIELDS = COMMON_FIELDS | {"projects", "limit", "cursor"}
GET_FIELDS = COMMON_FIELDS | {"projects", "session_uid"}
RANGE_FIELDS = COMMON_FIELDS | {"citation", "before", "after", "limit", "cursor"}
CITATION_FIELDS = frozenset({"session_uid", "source_generation", "first_sequence", "last_sequence", "event_ids"})
MAX_LIST = 50
LIST_DEFAULT = 20
RECORDS_DEFAULT = 50
MAX_RECORDS = 200
MAX_WIDEN = 20
CURSOR_SECONDS = 900.0
RESOLUTION_FAILURES = "evidence_reference_resolution_failures"
ALIAS_CALLS = "session_alias_calls"
ALIAS_OWNER = "agentihooks session memory"
ALIAS_EXPIRES = "2027-01-31T00:00:00Z"
ALIAS_EXPIRES_AT = datetime.fromisoformat(ALIAS_EXPIRES).timestamp()
ALIASES = {
    "list_sessions": "session_list",
    "get_session": "session_get",
    "get_session_segment": "session_records",
    "search_sessions": "session_search",
    "search_semantic": "session_search",
}
UNAVAILABLE = "the cited evidence is not available to this grant"
STALE_CITATION = "the cited records are not held as cited; search again for a current citation"
CURSOR_EXPIRED = "the cursor is unknown or expired; start the listing again without a cursor"
CURSOR_MOVED = "the listing changed since the cursor was issued; start the listing again without a cursor"
CURSOR_OTHER_REQUEST = "the cursor belongs to another request"
ALIAS_RETIRED = "this compatibility alias has expired; call its target tool"
UNKNOWN_TOOL = "no session tool has this name"
TOOL_FAILED = "the catalog failed before answering; the same request can be retried"


class Unresolved(Exception):
    pass


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _bounded(request: dict, name: str, default: int, low: int, high: int) -> int:
    value = request.get(name, default)
    if not _is_int(value) or not low <= value <= high:
        raise Rejected("invalid_request", f"{name} must be an integer from {low} to {high}")
    return value


def _envelope(request: object, fields: frozenset) -> dict:
    if not isinstance(request, dict):
        raise Rejected("invalid_request", "request must be an object")
    version = request.get("schema_version")
    if not isinstance(version, str) or version.split(".")[0] != SCHEMA_VERSION.split(".")[0]:
        raise Rejected("invalid_request", "unsupported schema_version")
    if set(request) - fields:
        raise Rejected("invalid_request", "request carries unknown fields")
    for name in fields & {"access_domain", "session_uid"}:
        if not isinstance(request.get(name), str) or not request[name]:
            raise Rejected("invalid_request", f"{name} must be a non-empty string")
    return request


def _citation(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != CITATION_FIELDS:
        raise Rejected("invalid_request", f"citation must carry exactly {sorted(CITATION_FIELDS)}")
    first, last, ids = value["first_sequence"], value["last_sequence"], value["event_ids"]
    if not (_is_int(first) and _is_int(last) and 1 <= first <= last):
        raise Rejected("invalid_request", "citation sequences must be positive and ordered")
    if not isinstance(ids, list) or not ids or not all(isinstance(item, str) and item for item in ids):
        raise Rejected("invalid_request", "citation event_ids must be a non-empty list of strings")
    if not all(isinstance(value[name], str) and value[name] for name in ("session_uid", "source_generation")):
        raise Rejected("invalid_request", "citation session_uid and source_generation must be non-empty strings")
    return value


def _refusal(operation: str, error: Rejected) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "operation_id": operation,
        "state": "rejected",
        "error": {"class": error.error_class, "retry": error.retry, "detail": error.detail},
    }


def _grant_digest(grant: Grant) -> str:
    return digest([grant.principal, grant.corpus, sorted(grant.projects)])


def _page(items: list, position: int, limit: int) -> tuple[list, int | None]:
    end = position + limit
    return items[position:end], end if end < len(items) else None


def citation(chunk: dict) -> dict:
    sequences = [item["sequence"] for item in chunk["evidence"]]
    return {
        "session_uid": chunk["session_uid"],
        "source_generation": chunk["source_generation"],
        "first_sequence": min(sequences),
        "last_sequence": max(sequences),
        "event_ids": [item["event_id"] for item in chunk["evidence"]],
    }


class SessionTools:
    def __init__(self, catalog: Catalog, store: archive.ObjectStore) -> None:
        self.catalog, self.store, self.searcher = catalog, store, core.SessionSearch(catalog)
        self.catalog.db.execute(SCHEMA)

    def _count(self, domain: str, name: str) -> None:
        self.catalog.db.execute(COUNT, (domain, name, 1))

    def counter(self, domain: str, name: str) -> int:
        row = self.catalog.db.execute(COUNTER, (domain, name)).fetchone()
        return row[0] if row else 0

    def _allowed(self, grant: Grant, request: dict) -> list[str]:
        projects = request.get("projects", [])
        if not isinstance(projects, list) or not all(isinstance(item, str) and item for item in projects):
            raise Rejected("invalid_request", "projects must be a list of non-empty strings")
        if request["access_domain"] != grant.corpus or not set(projects) <= grant.projects:
            self._count(grant.corpus, core.SCOPE_REJECTIONS)
            raise Rejected("forbidden_scope", core.SCOPE_REFUSED)
        return sorted(projects or grant.projects)

    def _visible(self, domain: str, projects: list[str], session_uid: str | None = None) -> list[tuple[str, str]]:
        allowed = json.dumps(projects)
        return [
            tuple(row) for row in self.catalog.db.execute(VISIBLE, (domain, session_uid, session_uid, allowed, allowed))
        ]

    def _summary(self, domain: str, session_uid: str, provider: str) -> dict:
        found = {
            source["source_generation"]: self.catalog.events(domain, session_uid, source["source_generation"])
            for source in self.catalog.sources(domain, session_uid)
        }
        events = [event for rows in found.values() for event in rows]
        sources = [
            {
                "source_generation": generation,
                "events": len(rows),
                "durable_through": self.catalog.watermark(domain, session_uid, generation),
            }
            for generation, rows in found.items()
        ]
        records = [event["record"] for event in events]
        stamps = sorted(record["timestamp"] for record in records if isinstance(record.get("timestamp"), str))
        return {
            "session_uid": session_uid,
            "harness": provider,
            "project_ids": sorted({event["project_id"] for event in events if event["project_id"]}),
            "task_ids": sorted({record["task_id"] for record in records if isinstance(record.get("task_id"), str)}),
            "first_timestamp": stamps[0] if stamps else None,
            "last_timestamp": stamps[-1] if stamps else None,
            "events": len(events),
            "sources": sources,
        }

    def _issue(self, grant: Grant, request: dict, position: int, fingerprint: str, now: float) -> str:
        domain, holder, asked = grant.corpus, _grant_digest(grant), digest({**request, "cursor": None})
        cursor_id = f"cursor-{digest([holder, asked, position, fingerprint])}"
        self.catalog.db.execute(PRUNE, (domain, now))
        self.catalog.db.execute(ISSUE, (domain, cursor_id, holder, asked, position, fingerprint, now + CURSOR_SECONDS))
        return cursor_id

    def _position(self, grant: Grant, request: dict, fingerprint: str, now: float) -> int:
        cursor_id = request.get("cursor")
        if cursor_id is None:
            return 0
        if not isinstance(cursor_id, str):
            raise Rejected("invalid_request", "cursor must be a string")
        row = self.catalog.db.execute(CURSOR, (grant.corpus, cursor_id)).fetchone()
        if row is None or row[0] != _grant_digest(grant) or row[4] <= now:
            raise Rejected("cursor_expired", CURSOR_EXPIRED)
        if row[1] != digest({**request, "cursor": None}):
            raise Rejected("invalid_request", CURSOR_OTHER_REQUEST)
        if row[3] != fingerprint:
            raise Rejected("cursor_expired", CURSOR_MOVED)
        return row[2]

    def _paged(self, grant: Grant, request: dict, items: list, limit: int, now: float) -> tuple[list, str | None]:
        fingerprint = digest(items)
        page, following = _page(items, self._position(grant, request, fingerprint, now), limit)
        return page, None if following is None else self._issue(grant, request, following, fingerprint, now)

    def session_list(self, grant: Grant, request: dict, now: float) -> dict:
        _envelope(request, LIST_FIELDS)
        projects = self._allowed(grant, request)
        limit = _bounded(request, "limit", LIST_DEFAULT, 1, MAX_LIST)
        visible = self._visible(grant.corpus, projects)
        page, cursor = self._paged(grant, request, [list(item) for item in visible], limit, now)
        sessions = [self._summary(grant.corpus, uid, provider) for uid, provider in page]
        return {"total": len(visible), "sessions": sessions, "next_cursor": cursor}

    def _resolve(self, grant: Grant, projects: list[str], session_uid: str) -> dict:
        found = self._visible(grant.corpus, projects, session_uid)
        if not found:
            raise Unresolved(UNAVAILABLE)
        return self._summary(grant.corpus, *found[0])

    def session_get(self, grant: Grant, request: dict, now: float) -> dict:
        _envelope(request, GET_FIELDS)
        projects = self._allowed(grant, request)
        session = self._resolve(grant, projects, request["session_uid"])
        scoped = Grant(grant.principal, grant.corpus, frozenset(projects))
        coverage = [
            metrics.Metrics(self.catalog).status(
                scoped, metrics.Scope(grant.corpus, session["session_uid"], source["source_generation"]), now
            )
            for source in session["sources"]
        ]
        return {"session": session, "coverage": coverage}

    def _chunk(self, domain: str, result: dict) -> dict:
        row = self.catalog.db.execute(CHUNK, (domain, result["session_uid"], result["chunk_id"])).fetchone()
        if row is None:
            raise Unresolved(STALE_CITATION)
        return json.loads(row[0])

    def session_search(self, grant: Grant, request: dict, embedder: Embedder | None = None) -> dict:
        answer = search_api.search(self.searcher, grant, request, embedder)
        if answer["state"] != "answered":
            raise Rejected(answer["error"]["class"], answer["error"]["detail"], answer["error"]["retry"])
        sessions: dict[str, dict] = {}
        for result in answer["results"]:
            chunk, uid = self._chunk(grant.corpus, result), result["session_uid"]
            if uid not in sessions:
                sessions[uid] = self._resolve(grant, sorted(grant.projects), uid)
            summary = sessions[uid]
            result["citation"], result["omitted_chars"] = citation(chunk), chunk["omitted_chars"]
            result["session"] = {
                key: summary[key] for key in ("harness", "task_ids", "first_timestamp", "last_timestamp")
            }
        return answer

    def _cited(self, grant: Grant, cited: dict, before: int, after: int) -> list[dict]:
        self._resolve(grant, sorted(grant.projects), cited["session_uid"])
        events = self.catalog.events(grant.corpus, cited["session_uid"], cited["source_generation"])
        held = {event["event_id"]: event["sequence"] for event in events}
        sequences = [held.get(event_id) for event_id in cited["event_ids"]]
        if None in sequences or (min(sequences), max(sequences)) != (cited["first_sequence"], cited["last_sequence"]):
            raise Unresolved(STALE_CITATION)
        low, high = cited["first_sequence"] - before, cited["last_sequence"] + after
        return [event for event in events if low <= event["sequence"] <= high]

    def _expanded(self, grant: Grant, cited: dict, event: dict) -> dict:
        reference = {
            "access_domain": grant.corpus,
            "session_uid": cited["session_uid"],
            "source_generation": cited["source_generation"],
            "byte_start": event["byte_start"],
            "byte_end": event["byte_end"],
        }
        raw = archive.source_range(self.catalog, self.store, grant, reference)
        record = event["record"]
        return {
            **{key: event[key] for key in ("event_id", "sequence", "byte_start", "byte_end", "project_id")},
            "cited": event["event_id"] in cited["event_ids"],
            "kind": record["kind"],
            "timestamp": record.get("timestamp"),
            "task_id": record.get("task_id"),
            "normalized_hash": event["normalized_hash"],
            "record": record,
            "source_text": raw.decode(errors="replace"),
        }

    def session_records(self, grant: Grant, request: dict, now: float) -> dict:
        _envelope(request, RANGE_FIELDS)
        if request["access_domain"] != grant.corpus:
            self._count(grant.corpus, core.SCOPE_REJECTIONS)
            raise Rejected("forbidden_scope", core.SCOPE_REFUSED)
        cited = _citation(request.get("citation"))
        before, after = _bounded(request, "before", 0, 0, MAX_WIDEN), _bounded(request, "after", 0, 0, MAX_WIDEN)
        limit = _bounded(request, "limit", RECORDS_DEFAULT, 1, MAX_RECORDS)
        events = self._cited(grant, cited, before, after)
        items = [[event["event_id"], event["normalized_hash"]] for event in events]
        page, cursor = self._paged(grant, request, items, limit, now)
        by_id = {event["event_id"]: event for event in events}
        records = [self._expanded(grant, cited, by_id[event_id]) for event_id, _ in page]
        return {
            "complete": cursor is None,
            "truncated": cursor is not None,
            "total": len(events),
            "records": records,
            "next_cursor": cursor,
        }


TOOLS = {
    "session_list": SessionTools.session_list,
    "session_get": SessionTools.session_get,
    "session_records": SessionTools.session_records,
}


def _target(tools: SessionTools, grant: Grant, name: str, now: float) -> tuple[str, dict]:
    if name not in ALIASES:
        return name, {}
    tools._count(grant.corpus, ALIAS_CALLS)
    if now >= ALIAS_EXPIRES_AT:
        raise Rejected("alias_expired", ALIAS_RETIRED)
    return ALIASES[name], {"name": name, "target": ALIASES[name], "owner": ALIAS_OWNER, "expires": ALIAS_EXPIRES}


def _answer(tools: SessionTools, grant: Grant, name: str, request: dict, now: float, embedder: Embedder | None):
    target, alias = _target(tools, grant, name, now)
    if target == "session_search":
        answer = tools.session_search(grant, request, embedder)
    elif target in TOOLS:
        answer = TOOLS[target](tools, grant, request, now)
    else:
        raise Rejected("invalid_request", UNKNOWN_TOOL)
    return {"tool": target, **({"alias": alias} if alias else {}), **answer}


def call(
    tools: SessionTools, name: str, grant: Grant, request: dict, now: float, embedder: Embedder | None = None
) -> dict:
    operation = f"{name}:{digest(request)[:16]}"
    try:
        answer = _answer(tools, grant, name, request, now, embedder)
    except Rejected as error:
        return _refusal(operation, error)
    except Unresolved as error:
        tools._count(grant.corpus, RESOLUTION_FAILURES)
        return _refusal(operation, Rejected("evidence_unavailable", str(error)))
    except archive.EvidenceRefused:
        tools._count(grant.corpus, RESOLUTION_FAILURES)
        return _refusal(operation, Rejected("evidence_unavailable", UNAVAILABLE))
    except archive.EvidenceIntegrityError as error:
        tools._count(grant.corpus, RESOLUTION_FAILURES)
        return _refusal(operation, Rejected("evidence_integrity", str(error)))
    except sqlite3.OperationalError:
        return _refusal(operation, Rejected("dependency_unavailable", TOOL_FAILED, retry="same_request"))
    return {**answer, "schema_version": SCHEMA_VERSION, "operation_id": operation, "state": "answered"}
