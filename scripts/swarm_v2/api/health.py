"""Controller health: shallow liveness, deep diagnostics of dependencies and pipeline progress, and the qualify gate."""

import hashlib
import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from scripts.swarm.store import RedisStore, SwarmError
from scripts.swarm_v2 import telemetry, worker_health
from scripts.swarm_v2.auth_context import GrantRefused

LIVE_PATH = "/v2/health/live"
DIAGNOSE_PATH = "/v2/health/diagnose"
QUALIFY_PATH = "/v2/health/qualify"
BEARER = "Bearer "
DEEP_ENV = "AGENTIHOOKS_HEALTH_DEEP"
SHALLOW, DEEP = "shallow", "deep"
OK, DEGRADED, DISABLED, UNKNOWN, STALLED = "ok", "degraded", "disabled", "unknown", "stalled"
UNAVAILABLE, AUTH_FAILED, ERROR, UNCONFIGURED = "unavailable", "auth_failed", "error", "unconfigured"
LIVE, SILENT = "live", "silent"
WORKING = "working"
BACKUP, NATIVE_RESUME = "backup", "native_resume"
PURPOSES = (BACKUP, NATIVE_RESUME)
ISSUED_LIMIT = 16
STATUS = {
    "invalid_request": 400,
    "unauthenticated": 401,
    "forbidden_scope": 403,
    "stale_generation": 409,
    "revision_conflict": 409,
    "dependency_unavailable": 503,
}
REFUSALS = {
    "unsupported_purpose": "health qualifies only backup or native resume",
    "shallow_report": "a shallow liveness report cannot qualify backup or native resume",
    "unrecognized_report": "the report was not issued by this controller or was altered",
    "stale_report": "the report is older than the qualifying window",
    "dependency_unavailable": "Redis was not available when the report was taken",
    "pipeline_not_ok": "the pipeline stage this purpose needs is not ok",
}
HINTS = {
    ("process", "lease_absent"): (
        "controller_standby",
        "This controller holds no swarm lease, so it admits and recovers nothing; check which controller holds it.",
    ),
    ("redis", UNAVAILABLE): (
        "dependency_unavailable",
        "Check the Redis service and the controller network path; claims, heartbeats and commands are refused "
        "until Redis answers.",
    ),
    ("brain", AUTH_FAILED): (
        "credential_rejected",
        "The brain refused the controller credential; reissue the brain token for this swarm through its secret. "
        "Nothing retries with another credential.",
    ),
    ("brain", UNAVAILABLE): (
        "dependency_unavailable",
        "Check the brain API service and its network path; context delivery stays degraded until it answers.",
    ),
    ("brain", ERROR): ("dependency_error", "Read the brain API logs for the failing health request."),
    ("transition_export", DEGRADED): (
        "pipeline_degraded",
        "Check the transition exporter; missed events replay from the durable watermark once it accepts them.",
    ),
    ("archive", STALLED): (
        "pipeline_stalled",
        "The execution heartbeats but its archive watermark has not advanced while it works; inspect its "
        "transcript exporter. The last durable watermark is kept.",
    ),
}


class HealthRefused(SwarmError):
    def __init__(self, reason: str) -> None:
        super().__init__(REFUSALS[reason])
        self.reason = reason


@dataclass(frozen=True)
class Limits:
    fresh_ms: int = 120_000
    stall_ms: int = 600_000
    report_ms: int = 30_000
    brain_interval_ms: int = 30_000


def deep_enabled(environ: Mapping[str, str] = os.environ) -> bool:
    return str(environ.get(DEEP_ENV)).lower() != "off"


def brain_probe(environ: Mapping[str, str], timeout: float = worker_health.BRAIN_TIMEOUT) -> Callable[[], str]:
    return lambda: worker_health.brain_state(dict(environ), timeout)


def classify_brain(answer: str) -> str:
    if answer in (OK, UNCONFIGURED):
        return answer
    if answer in ("brain_http_401", "brain_http_403"):
        return AUTH_FAILED
    return UNAVAILABLE if answer == "brain_unreachable" else ERROR


def pipeline_stage_health(report: dict) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    pipeline = report["pipeline"]
    readings = [("archive", entry) for entry in pipeline["archive"]]
    if "transition_export" in pipeline:
        readings.append(("transition_export", pipeline["transition_export"]))
    for stage, reading in readings:
        states = counts.setdefault(stage, {})
        states[reading["state"]] = states.get(reading["state"], 0) + 1
    return counts


def _digest(report: dict) -> str:
    return hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()


def _unknown(reading: dict) -> dict:
    return {**reading, "state": UNKNOWN, "last_observed_state": reading["state"]}


def _hint(component: str, condition: str, **fields: str) -> dict:
    error_class, text = HINTS[(component, condition)]
    return {
        "component": component,
        "condition": condition,
        "class": error_class,
        "hint": text,
        "automatic": False,
        **fields,
    }


class Health:
    """Liveness touches no dependency; diagnostics never act on what they find."""

    def __init__(
        self,
        store: RedisStore,
        slug: str,
        brain: Callable[[], str] | None = None,
        controller: object | None = None,
        limits: Limits = Limits(),
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store, self.slug, self.brain, self.controller, self.limits, self.clock = (
            store,
            slug,
            brain,
            controller,
            limits,
            clock,
        )
        self.pid = os.getpid()
        self.started_ms = self._now()
        self.dependencies = {name: {"last_ok_at_ms": 0} for name in ("redis", "brain")}
        self.brain_at: int | None = None
        self.readings: dict | None = None
        self.issued: dict[str, str] = {}
        self.lock = threading.Lock()

    def _now(self) -> int:
        return int(self.clock() * 1000)

    def live(self) -> dict:
        return {
            "probe": SHALLOW,
            "status": LIVE,
            "pid": self.pid,
            "started_at_ms": self.started_ms,
            "checked_at_ms": self._now(),
            "qualifies": [],
        }

    def diagnose(self, execution_id: str = "") -> dict:
        now = self._now()
        self._check_brain(now)
        pipeline = self._pipeline(now)
        if execution_id:
            pipeline = {"archive": [e for e in pipeline["archive"] if e["execution_id"] == execution_id]}
        process = self._process()
        remediation = self._remediation(process, pipeline)
        report = {
            "probe": DEEP,
            "report_id": uuid.uuid4().hex,
            "checked_at_ms": now,
            "scope": "execution" if execution_id else "swarm",
            "status": DEGRADED if remediation else OK,
            "process": process,
            "dependencies": {name: dict(found) for name, found in self.dependencies.items()},
            "pipeline": pipeline,
            "remediation": remediation,
        }
        with self.lock:
            self.issued[report["report_id"]] = _digest(report)
            while len(self.issued) > ISSUED_LIMIT:
                del self.issued[next(iter(self.issued))]
        return report

    def qualify(self, report: dict, purpose: str, execution_id: str = "") -> dict:
        if purpose not in PURPOSES:
            raise HealthRefused("unsupported_purpose")
        if report.get("probe") != DEEP:
            raise HealthRefused("shallow_report")
        with self.lock:
            issued = self.issued.get(report.get("report_id"))
        if issued != _digest(report):
            raise HealthRefused("unrecognized_report")
        if self._now() - report["checked_at_ms"] > self.limits.report_ms:
            raise HealthRefused("stale_report")
        if report["dependencies"]["redis"]["state"] != OK:
            raise HealthRefused("dependency_unavailable")
        archive = report["pipeline"]["archive"]
        if purpose == NATIVE_RESUME:
            ready = any(e["execution_id"] == execution_id and e["state"] == OK for e in archive)
        else:
            export = report["pipeline"].get("transition_export", {}).get("state")
            ready = export in (OK, DISABLED) and all(e["state"] == OK for e in archive)
        if not ready:
            raise HealthRefused("pipeline_not_ok")
        return {
            "purpose": purpose,
            "qualified": True,
            "execution_id": execution_id,
            "report_id": report["report_id"],
            "checked_at_ms": report["checked_at_ms"],
        }

    def _record(self, name: str, state: str, reason: str, now: int) -> None:
        found = self.dependencies[name]
        found.update(state=state, reason=reason, checked_at_ms=now)
        if state == OK:
            found["last_ok_at_ms"] = now

    def _check_brain(self, now: int) -> None:
        if self.brain_at is not None and now - self.brain_at < self.limits.brain_interval_ms:
            return
        self.brain_at = now
        answer = self.brain() if self.brain is not None else UNCONFIGURED
        self._record("brain", classify_brain(answer), answer, now)

    def _pipeline(self, now: int) -> dict:
        try:
            readings = {"transition_export": self._export(now), "archive": self._archive(now)}
        except Exception as error:
            self._record("redis", UNAVAILABLE, telemetry.sanitize_error(error), now)
            return self._last_readings()
        self._record("redis", OK, "", now)
        self.readings = readings
        return readings

    def _last_readings(self) -> dict:
        if self.readings is None:
            return {"archive": [], "transition_export": {"state": UNKNOWN}}
        return {
            "transition_export": _unknown(self.readings["transition_export"]),
            "archive": [_unknown(entry) for entry in self.readings["archive"]],
        }

    def _export(self, now: int) -> dict:
        found = telemetry.observability(self.store, self.slug)
        exported = self.store.redis.get(self.store.key(self.slug, telemetry.EXPORTED))
        return {
            "state": found["state"],
            "watermark": int(exported or 0),
            "backlog": found["unexported"],
            "exported_at_ms": found["exported_at_ms"],
            "failed_at_ms": found["failed_at_ms"],
            "last_error": found["last_error"],
            "observed_at_ms": now,
        }

    def _archive(self, now: int) -> list[dict]:
        beats = self.store.redis.hgetall(self.store.key(self.slug, "heartbeats"))
        entries = []
        for seat, agent in sorted(self.store.execution_occupants(self.slug).items()):
            raw = beats.get(agent.execution_id)
            if not raw:
                continue
            beat = json.loads(raw)
            accepted = beat["accepted_at_ms"]
            advanced = beat.get("archive_advanced_at_ms", accepted)
            process = LIVE if now - accepted <= self.limits.fresh_ms else SILENT
            state = UNKNOWN if process == SILENT else OK
            if process == LIVE and beat["state"] == WORKING and now - advanced > self.limits.stall_ms:
                state = STALLED
            entries.append(
                {
                    "execution_id": agent.execution_id,
                    "seat": seat,
                    "process": process,
                    "activity": beat["state"],
                    "heartbeat_at_ms": accepted,
                    "watermark": beat["archive_watermark"],
                    "advanced_at_ms": advanced,
                    "state": state,
                    "observed_at_ms": now,
                }
            )
        return entries

    def _process(self) -> dict:
        if self.controller is None:
            lease = "unmanaged"
        else:
            lease = "absent" if self.controller.held is None else "held"
        return {"status": LIVE, "pid": self.pid, "started_at_ms": self.started_ms, "controller_lease": lease}

    def _remediation(self, process: dict, pipeline: dict) -> list[dict]:
        hints = [_hint("process", "lease_absent")] if process["controller_lease"] == "absent" else []
        for name, found in self.dependencies.items():
            if (name, found["state"]) in HINTS:
                hints.append(_hint(name, found["state"]))
        export = pipeline.get("transition_export", {}).get("state")
        if ("transition_export", export) in HINTS:
            hints.append(_hint("transition_export", export))
        for entry in pipeline["archive"]:
            if ("archive", entry["state"]) in HINTS:
                hints.append(_hint("archive", entry["state"], execution_id=entry["execution_id"]))
        return hints


class HealthAPI:
    """Liveness is open; diagnostics need the operator credential or a worker grant, scoped to that execution."""

    def __init__(
        self,
        health: Health,
        grants: object | None = None,
        operator: Callable[[str, str], object] | None = None,
        deep: bool = True,
    ) -> None:
        self.health, self.grants, self.operator, self.deep = health, grants, operator, deep

    def route(self, method: str, path: str, authorization: str, body: object) -> tuple[int, dict]:
        if (method, path) == ("GET", LIVE_PATH):
            return 200, self.health.live()
        if (method, path) not in (("GET", DIAGNOSE_PATH), ("POST", QUALIFY_PATH)):
            return 404, GrantRefused("invalid_request", "no such health endpoint").detail()
        if not self.deep:
            return 503, GrantRefused("dependency_unavailable", "deep diagnostics are switched off").detail()
        try:
            scope = self._scope(authorization)
            if path == DIAGNOSE_PATH:
                return 200, self.health.diagnose(scope)
            if scope:
                raise GrantRefused("forbidden_scope", "only the operator can qualify backup or native resume")
            return self._qualify(body)
        except GrantRefused as error:
            return STATUS[error.error_class], error.detail()

    def _scope(self, authorization: str) -> str:
        if not authorization.startswith(BEARER):
            raise GrantRefused("unauthenticated", "a bearer credential is required")
        token = authorization.removeprefix(BEARER)
        try:
            if self.operator is not None and self.operator(self.health.slug, token):
                return ""
            if self.grants is None:
                raise GrantRefused("unauthenticated", "the credential is not recognized")
            return self.grants.bound(self.health.slug, token).execution_id
        except GrantRefused:
            raise
        except Exception:
            raise GrantRefused("dependency_unavailable", "the credential store is unavailable") from None

    def _qualify(self, body: object) -> tuple[int, dict]:
        fields = body if isinstance(body, dict) else {}
        purpose, report, execution_id = fields.get("purpose"), fields.get("report"), fields.get("execution_id", "")
        if not isinstance(purpose, str) or not isinstance(report, dict) or not isinstance(execution_id, str):
            raise GrantRefused("invalid_request", "a qualify request needs a purpose, a report and an execution id")
        try:
            return 200, self.health.qualify(report, purpose, execution_id)
        except HealthRefused as refused:
            return 409, {"error_class": "not_qualified", "reason": refused.reason, "message": str(refused)}
