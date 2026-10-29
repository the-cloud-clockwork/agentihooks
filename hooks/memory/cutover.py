"""Shadow search qualification and versioned consumer routes for cutting search consumers over to session tools.

A consumer qualifies only when every expected source range is found by search and expanded to its records under
the caller's own grant; ranking scores are never compared. Routes are durable compare-and-set revisions, and a
rollback restores the previous accepted route without touching capture or the recorded qualification evidence.
"""

import json
import sqlite3
from pathlib import Path

from hooks.memory.api import records
from hooks.memory.api.ingest import SCHEMA_VERSION, Grant, digest

LOCAL, SESSION = "local.v1", "session.v1"
PROTOCOLS = {SESSION: "1"}
ENDPOINTS = frozenset({LOCAL, *PROTOCOLS})
SEARCH_LIMIT = 50
RECORD_PAGE = records.MAX_RECORDS
SCHEMA = (
    """CREATE TABLE IF NOT EXISTS consumer_route (
    access_domain TEXT NOT NULL, consumer TEXT NOT NULL, revision INTEGER NOT NULL, endpoint TEXT NOT NULL,
    action TEXT NOT NULL, operation_id TEXT, principal TEXT NOT NULL, PRIMARY KEY (access_domain, consumer, revision))""",
    """CREATE TABLE IF NOT EXISTS shadow_evidence (
    access_domain TEXT NOT NULL, operation_id TEXT NOT NULL, holder TEXT NOT NULL, proof TEXT NOT NULL,
    PRIMARY KEY (access_domain, operation_id))""",
)
ROUTE = (
    "SELECT revision, endpoint, action, operation_id FROM consumer_route WHERE access_domain = ? AND consumer = ? "
    "ORDER BY revision DESC LIMIT 1"
)
PREVIOUS = "SELECT endpoint FROM consumer_route WHERE access_domain = ? AND consumer = ? AND revision = ?"
WRITE = "INSERT INTO consumer_route VALUES (?, ?, ?, ?, ?, ?, ?)"
EVIDENCE = "SELECT holder, proof FROM shadow_evidence WHERE access_domain = ? AND operation_id = ?"
RECORD = "INSERT OR IGNORE INTO shadow_evidence VALUES (?, ?, ?, ?)"


def _holder(grant: Grant) -> str:
    return digest([grant.principal, grant.corpus, sorted(grant.projects)])


def _ranges(question: dict) -> list[tuple]:
    expected = question.get("expected")
    if not isinstance(expected, list) or not expected:
        raise ValueError(f"question {question['id']!r} names no expected source range")
    keys = ("session_uid", "source_generation", "first_sequence", "last_sequence")
    ranges = [tuple(item[key] for key in keys) for item in expected]
    if not all(isinstance(first, int) and isinstance(last, int) and 1 <= first <= last for *_, first, last in ranges):
        raise ValueError(f"question {question['id']!r} names a range that is not positive and ordered")
    return ranges


def _expand(tools: records.SessionTools, grant: Grant, cited: dict, now: float) -> list[dict] | None:
    asked = {"schema_version": SCHEMA_VERSION, "access_domain": grant.corpus, "citation": cited, "limit": RECORD_PAGE}
    expanded, found = records.call(tools, "session_records", grant, asked, now), []
    while expanded["state"] == "answered":
        found += expanded["records"]
        if expanded["next_cursor"] is None:
            return found
        expanded = records.call(tools, "session_records", grant, {**asked, "cursor": expanded["next_cursor"]}, now)
    return None


def _held(tools: records.SessionTools, grant: Grant, text: str, now: float) -> tuple[set, int, int]:
    request = {"schema_version": SCHEMA_VERSION, "access_domain": grant.corpus, "query": text, "limit": SEARCH_LIMIT}
    found = records.call(tools, "session_search", grant, request, now)
    if found["state"] != "answered":
        return set(), 1, 0
    held, refused, foreign = set(), 0, 0
    for result in found["results"]:
        cited = result["citation"]
        expanded = _expand(tools, grant, cited, now)
        if expanded is None:
            refused += 1
            continue
        for record in expanded:
            if record["project_id"] and record["project_id"] not in grant.projects:
                foreign += 1
                continue
            held.add((cited["session_uid"], cited["source_generation"], record["sequence"]))
    return held, refused, foreign


def shadow(tools: records.SessionTools, grant: Grant, questions: list[dict], now: float) -> dict:
    if not questions:
        raise ValueError("a shadow replay needs at least one question")
    asked = [(question, _ranges(question)) for question in questions]
    expected = recalled = unexpandable = unauthorized = 0
    for question, ranges in asked:
        held, refused, foreign = _held(tools, grant, question["text"], now)
        unexpandable, unauthorized = unexpandable + refused, unauthorized + foreign
        for uid, generation, first, last in ranges:
            expected += 1
            recalled += all((uid, generation, sequence) in held for sequence in range(first, last + 1))
    qualified = recalled == expected and not unauthorized
    return {
        "schema_version": SCHEMA_VERSION,
        "state": "accepted" if qualified else "rejected",
        "questions": len(asked),
        "expected_ranges": expected,
        "recalled_ranges": recalled,
        "unresolved_ranges": expected - recalled,
        "unexpandable_results": unexpandable,
        "unauthorized_results": unauthorized,
        "shadow_search_source_recall": recalled / expected,
    }


class ConsumerRoutes:
    def __init__(self, path: Path) -> None:
        self.db = sqlite3.connect(path, isolation_level=None)
        for statement in SCHEMA:
            self.db.execute(statement)

    def close(self) -> None:
        self.db.close()

    def route(self, grant: Grant, consumer: str) -> dict:
        row = self.db.execute(ROUTE, (grant.corpus, consumer)).fetchone()
        revision, endpoint, action, operation = row or (0, LOCAL, "initial", None)
        return {
            "consumer": consumer,
            "endpoint": endpoint,
            "revision": revision,
            "action": action,
            "operation_id": operation,
        }

    def qualify(self, tools: records.SessionTools, grant: Grant, consumer: str, questions: list, now: float) -> dict:
        answer = shadow(tools, grant, questions, now)
        operation = f"shadow-{digest([consumer, _holder(grant), questions, now, answer])[:16]}"
        proof = {"operation_id": operation, "consumer": consumer, "observed_at": now, **answer}
        self.db.execute(RECORD, (grant.corpus, operation, _holder(grant), json.dumps(proof)))
        return self.evidence(grant, operation)

    def evidence(self, grant: Grant, operation: str) -> dict:
        row = self.db.execute(EVIDENCE, (grant.corpus, operation)).fetchone()
        if row is None or row[0] != _holder(grant):
            raise ValueError("no shadow evidence with this operation for this grant")
        return json.loads(row[1])

    def _write(self, grant: Grant, consumer: str, expected: int, change: tuple) -> dict:
        try:
            if self.route(grant, consumer)["revision"] != expected:
                raise sqlite3.IntegrityError
            self.db.execute(WRITE, (grant.corpus, consumer, expected + 1, *change, grant.principal))
        except sqlite3.IntegrityError:
            raise ValueError("the route changed since the given revision; read it again") from None
        return self.route(grant, consumer)

    def switch(self, grant: Grant, consumer: str, endpoint: str, expected: int, operation: str) -> dict:
        if endpoint not in ENDPOINTS:
            raise ValueError(f"unsupported consumer endpoint {endpoint!r}")
        proof = self.evidence(grant, operation)
        if proof["consumer"] != consumer or proof["state"] != "accepted":
            raise ValueError("the shadow evidence does not qualify this consumer")
        current = self.route(grant, consumer)
        done = {"endpoint": endpoint, "revision": expected + 1, "action": "switch", "operation_id": operation}
        if all(current[key] == value for key, value in done.items()):
            return current
        return self._write(grant, consumer, expected, (endpoint, "switch", operation))

    def rollback(self, grant: Grant, consumer: str, expected: int) -> dict:
        current = self.route(grant, consumer)
        if current["revision"] == expected + 1 and current["action"] == "rollback":
            return current
        if current["action"] != "switch":
            raise ValueError("there is no switched route to roll back")
        row = self.db.execute(PREVIOUS, (grant.corpus, consumer, expected - 1)).fetchone()
        previous = row[0] if row else LOCAL
        return self._write(grant, consumer, expected, (previous, "rollback", current["operation_id"]))

    def call(self, endpoints: dict, grant: Grant, consumer: str, name: str, request: dict, now: float) -> dict:
        endpoint = self.route(grant, consumer)["endpoint"]
        if endpoint not in endpoints:
            raise ValueError(f"consumer {consumer!r} routes to {endpoint!r}, which this caller does not serve")
        if str(request.get("schema_version")).split(".")[0] != PROTOCOLS[endpoint]:
            raise ValueError(f"endpoint {endpoint!r} accepts only protocol {PROTOCOLS[endpoint]} requests")
        return records.call(endpoints[endpoint], name, grant, {**request, "schema_version": SCHEMA_VERSION}, now)
