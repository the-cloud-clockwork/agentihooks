import json
import logging
import math
import os
import sqlite3
import time
from collections import Counter
from dataclasses import replace
from functools import wraps
from pathlib import Path
from uuid import uuid4

from hooks.context import project_cache
from hooks.memory.api.ingest import Grant
from scripts.gates.base import Call, Who
from scripts.swarm_v2.keyspace import Namespace, key

SCHEMA = "CREATE TABLE IF NOT EXISTS consultation_event (scope TEXT, id TEXT, data TEXT, PRIMARY KEY(scope, id))"
COMMON = {"kind", "session", "task", "context"}
FIELDS = {
    "delivery": COMMON | {"purpose", "target", "pack_bytes", "delivery", "retrieval", "receipt_age_seconds"},
    "gate": COMMON | {"purpose", "gate_result", "receipt_age_seconds"},
    "reference": COMMON | {"lesson", "incident"},
}
CHOICES = {
    "purpose": {"task_transition", "search", "refresh"},
    "target": {"claude", "codex", "copilot"},
    "delivery": {"delivered", "disabled", "unsupported_event", "missing", "deduplicated"},
    "retrieval": {"delivered", "empty", "failed", "missing"},
    "gate_result": {"allow", "deny", "observe", "lifted", "fail-open", "off"},
}
OPAQUE = {"session", "task", "context", "lesson", "incident"}
LOG = logging.getLogger(__name__)


class Metrics:
    def __init__(self, path: Path, namespace: Namespace, operators: frozenset[str] = frozenset()) -> None:
        self.path, self.namespace, self.operators = path, namespace, operators
        self.scope = key(namespace, "consultation-metrics")

    def _authorize(self, grant: Grant) -> None:
        if grant.corpus != self.namespace.brain or self.namespace.project not in grant.projects:
            raise PermissionError("consultation metrics scope refused")
        if self.namespace.project in {"", "unknown"}:
            raise PermissionError("consultation metrics require an attributed project")

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path)
        db.execute(SCHEMA)
        return db

    def _opaque(self, kind: str, value: str) -> str:
        return key(self.namespace, kind, value)

    def _event(self, grant: Grant, event: dict) -> dict:
        kind = event.get("kind")
        if kind not in FIELDS or set(event) != FIELDS[kind]:
            raise ValueError("invalid consultation metric fields")
        if event["session"] != grant.principal:
            raise PermissionError("consultation metric actor refused")
        for name, choices in CHOICES.items():
            if name in event and event[name] not in choices:
                raise ValueError("invalid consultation metric category")
        if "pack_bytes" in event and (type(event["pack_bytes"]) is not int or event["pack_bytes"] < 0):
            raise ValueError("invalid consultation pack size")
        age = event.get("receipt_age_seconds")
        if age is not None and (type(age) not in {int, float} or not math.isfinite(age) or age < 0):
            raise ValueError("invalid consultation receipt age")
        identifiers = OPAQUE & event.keys()
        if any(not isinstance(event[name], str) or not event[name] for name in identifiers):
            raise ValueError("invalid consultation identifier")
        return {name: self._opaque(name, value) if name in identifiers else value for name, value in event.items()}

    def _insert(self, identifier: str, event: dict) -> str:
        data = json.dumps(event, sort_keys=True)
        db = self._connect()
        try:
            with db:
                db.execute("INSERT OR IGNORE INTO consultation_event VALUES (?, ?, ?)", (self.scope, identifier, data))
                stored = db.execute(
                    "SELECT data FROM consultation_event WHERE scope = ? AND id = ?", (self.scope, identifier)
                ).fetchone()[0]
                if stored != data:
                    raise ValueError("consultation operation replay differs")
        finally:
            db.close()
        return identifier

    def events(self, grant: Grant) -> list[dict]:
        self._authorize(grant)
        db = self._connect()
        try:
            return [
                {"id": identifier, **json.loads(data)}
                for identifier, data in db.execute(
                    "SELECT id, data FROM consultation_event WHERE scope = ? ORDER BY rowid", (self.scope,)
                )
            ]
        finally:
            db.close()

    def record(self, grant: Grant, operation: str, event: dict) -> str:
        self._authorize(grant)
        normalized = self._event(grant, event)
        if normalized["kind"] == "reference" and not any(
            _delivered(row) and all(row[name] == normalized[name] for name in ("session", "task", "context"))
            for row in self.events(grant)
        ):
            raise ValueError("reference requires delivered context")
        return self._insert(self._opaque("operation", json.dumps([grant.principal, operation])), normalized)

    def review(self, grant: Grant, identifier: str, assessment: dict) -> str:
        self._authorize(grant)
        if grant.principal not in self.operators:
            raise PermissionError("consultation review requires an operator grant")
        if not assessment or set(assessment) - {"usefulness", "false_block", "missed_opportunity"}:
            raise ValueError("invalid consultation review fields")
        target = next((row for row in self.events(grant) if row["id"] == identifier), None)
        if not target or target["kind"] == "review":
            raise ValueError("consultation review target unavailable")
        _assessment(target, assessment)
        return self._insert(
            self._opaque("review", identifier),
            {"kind": "review", "event": identifier, "actor": self._opaque("actor", grant.principal), **assessment},
        )

    def report(self, grant: Grant) -> dict:
        rows = self.events(grant)
        deliveries = [row for row in rows if row["kind"] == "delivery"]
        gates = [row for row in rows if row["kind"] == "gate"]
        reviews = [row for row in rows if row["kind"] == "review"]
        reviewed_gates = {row["id"] for row in gates if row["gate_result"] == "deny"}
        judged_blocks = [row for row in reviews if row["event"] in reviewed_gates and "false_block" in row]
        references = [row for row in rows if row["kind"] == "reference"]
        latest = {row["session"]: row for row in deliveries if row["delivery"] != "deduplicated"}
        correlations = Counter((row["incident"], row["lesson"]) for row in references)
        return {
            "context_delivered": len(
                {(row["session"], row["task"], row["context"]) for row in deliveries if _delivered(row)}
            ),
            "context_referenced": len({(row["session"], row["task"], row["context"]) for row in references}),
            "operator_reviewed_useful": sum(row.get("usefulness") == "useful" for row in reviews),
            "operator_reviewed_irrelevant": sum(row.get("usefulness") == "irrelevant" for row in reviews),
            "operator_reviewed_missed_opportunities": sum(row.get("missed_opportunity", False) for row in reviews),
            "brain_gate_false_block_rate": (
                sum(row["false_block"] for row in judged_blocks) / len(judged_blocks) if judged_blocks else None
            ),
            "operator_overrides": sum(row["gate_result"] == "lifted" for row in gates),
            "context_delivery_failures": sum(
                row["delivery"] in {"disabled", "unsupported_event", "missing"} for row in deliveries
            ),
            "context_service_failures": sum(row["retrieval"] == "failed" for row in deliveries),
            "delivery_outcomes": dict(Counter(row["delivery"] for row in deliveries)),
            "gate_results": dict(Counter(row["gate_result"] for row in gates)),
            "degraded": any(not _delivered(row) for row in latest.values()),
            "correlations": [
                {"incident": incident, "lesson": lesson, "references": count}
                for (incident, lesson), count in sorted(correlations.items())
            ],
        }


def _delivered(row: dict) -> bool:
    return row.get("delivery") == "delivered" and row.get("retrieval") in {"delivered", "empty"}


def _assessment(target: dict, assessment: dict) -> None:
    if "usefulness" in assessment:
        if not _delivered(target) or assessment["usefulness"] not in {"useful", "irrelevant", "unknown"}:
            raise ValueError("usefulness requires delivered context and a valid assessment")
    for name, results in (("false_block", {"deny"}), ("missed_opportunity", {"allow", "observe", "lifted"})):
        if name in assessment and (type(assessment[name]) is not bool or target.get("gate_result") not in results):
            raise ValueError("invalid gate assessment")


def runtime_namespace() -> Namespace:
    from hooks.context.project_identity import resolve_project

    identity = resolve_project(os.getcwd())
    return replace(project_cache.namespace(), project=identity.project_id if identity else "unknown")


def _best_effort(function):
    @wraps(function)
    def observe(*args, **kwargs):
        try:
            function(*args, **kwargs)
        except Exception:
            LOG.warning("consultation metrics unavailable")

    return observe


def emit(session: str, operation: str, event: dict) -> None:
    from hooks.config import AGENTIHOOKS_HOME

    namespace = runtime_namespace()
    if namespace.project in {"", "unknown"}:
        return
    grant = Grant(session, namespace.brain, frozenset({namespace.project}))
    Metrics(Path(AGENTIHOOKS_HOME) / "brain/consultation.sqlite3", namespace).record(grant, operation, event)


@_best_effort
def delivery(session: str, pack: str | None, outcome: str) -> None:
    from hooks.context import task_context
    from hooks.targets import current_target

    receipt = task_context.consultation(session)
    task = os.environ.get("AGENTIHOOKS_SWARM_TASK")
    if not task:
        return
    context = json.dumps(
        [task, receipt.get("generation"), receipt.get("revision"), receipt.get("digest"), receipt.get("at")]
    )
    emit(
        session,
        f"delivery:{uuid4()}",
        {
            "kind": "delivery",
            "session": session,
            "task": task,
            "context": context,
            "purpose": "task_transition",
            "target": current_target(),
            "pack_bytes": len((pack or "").encode()),
            "delivery": outcome,
            "retrieval": task_context.classify(pack) if pack else "missing",
            "receipt_age_seconds": max(0, time.time() - receipt["at"]) if receipt else None,
        },
    )


@_best_effort
def gate(call: Call, who: Who, result: str) -> None:
    from hooks.context import task_context

    if not (who.swarm and who.task):
        return
    receipt = task_context.consultation(call.session)
    context = json.dumps(
        [who.task, receipt.get("generation"), receipt.get("revision"), receipt.get("digest"), receipt.get("at")]
    )
    emit(
        call.session,
        f"gate:{uuid4()}",
        {
            "kind": "gate",
            "session": call.session,
            "task": who.task,
            "context": context,
            "purpose": "task_transition",
            "gate_result": result,
            "receipt_age_seconds": max(0, time.time() - receipt["at"]) if receipt else None,
        },
    )
