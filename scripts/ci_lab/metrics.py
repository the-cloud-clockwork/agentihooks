"""Prometheus exposition for GET /metrics and the lab_units and lab_grades ClickHouse rows."""

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from math import inf, isfinite, isnan
from typing import Protocol
from urllib.parse import urlsplit

from scripts.ci_lab import contract
from scripts.ci_lab.capacity import Load, document, predicted_wait
from scripts.ci_lab.delivery import CLASSES
from scripts.swarm import metrics_outbox

__all__ = [
    "CONTRACT",
    "GRADES",
    "LEDGER",
    "UNITS",
    "GradeRecord",
    "Metric",
    "Registry",
    "UnitRecord",
    "grade_row",
    "observe_load",
    "observe_records",
    "record",
    "respond",
    "serve",
    "unit_row",
]

LEDGER = "ci-lab"
JSON = "application/json"
EXPOSITION = "text/plain; version=0.0.4; charset=utf-8"
ROUTES = ("/capacity", "/metrics")


@dataclass(frozen=True)
class Metric:
    """One contract metric: its name, Prometheus type, label names and help text."""

    name: str
    kind: str
    labels: tuple[str, ...]
    help: str


HELP = {
    contract.UNITS_QUEUED: "Units pending per class.",
    contract.UNITS_RUNNING: "Units leased and running per class.",
    contract.SLOTS_TOTAL: "Slots offered by ready workers.",
    contract.SLOTS_BUSY: "Slots running a unit.",
    contract.SLOTS_WANTED: "Slots the queue asks the autoscaler for.",
    contract.QUEUE_WAIT_SECONDS: "Predicted queue wait per class.",
    contract.UNIT_DURATION_SECONDS: "Last unit duration per kind.",
    contract.CACHE_HITS_TOTAL: "Cache hits per cache.",
    contract.CACHE_MISSES_TOTAL: "Cache misses per cache.",
    contract.MIRROR_AGE_SECONDS: "Age of the git mirror.",
    contract.CORE_SECONDS_TOTAL: "Core seconds spent per class.",
    contract.HEADS_GRADED_TOTAL: "Heads graded per class and conclusion.",
    contract.BUDGET_FRACTION: "Fraction of the daily core budget left.",
    contract.CLASS_SHED: "One when a class is shed.",
    contract.UNITS_REDELIVERED_TOTAL: "Units requeued after an expired lease.",
    contract.UNITS_LOST_TOTAL: "Units lost after a second expired lease.",
    contract.UNITS_OVERDUE: "Leased units past their estimate.",
    contract.WORKERS_READY: "Workers holding a live heartbeat.",
    contract.GITHUB_RATE_REMAINING: "GitHub API calls left in the window.",
    contract.VERDICT_MISMATCH_TOTAL: "Shadow verdicts that differ from Actions.",
}
COUNTERS = frozenset(
    {
        contract.CACHE_HITS_TOTAL,
        contract.CACHE_MISSES_TOTAL,
        contract.CORE_SECONDS_TOTAL,
        contract.HEADS_GRADED_TOTAL,
        contract.UNITS_REDELIVERED_TOTAL,
        contract.UNITS_LOST_TOTAL,
        contract.VERDICT_MISMATCH_TOTAL,
    }
)
CONTRACT = tuple(
    Metric(name, "counter" if name in COUNTERS else "gauge", labels, HELP[name])
    for name, labels in contract.METRICS.items()
)
BY_NAME = {metric.name: metric for metric in CONTRACT}

UNITS = metrics_outbox.Table(
    "lab_units",
    (
        ("repo", "String"),
        ("head", "String"),
        ("unit_key", "String"),
        ("kind", "String"),
        ("class", "String"),
        ("worker", "String"),
        ("conclusion", "String"),
        ("cached", "Int64"),
        ("seconds", "Float64"),
        ("wait_s", "Float64"),
    ),
)
GRADES = metrics_outbox.Table(
    "lab_grades",
    (
        ("repo", "String"),
        ("head", "String"),
        ("base", "String"),
        ("class", "String"),
        ("conclusion", "String"),
        ("units", "Int64"),
        ("cached_units", "Int64"),
        ("seconds", "Float64"),
    ),
)


class Sink(Protocol):
    """The metrics outbox surface the lab writes through."""

    def append(self, table: metrics_outbox.Table, rows: list[dict[str, object]]) -> object:
        """Spool rows for one table."""
        ...


def _labels(metric: Metric, labels: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    if set(labels) != set(metric.labels):
        raise ValueError(f"{metric.name} takes the labels {list(metric.labels)}, got {sorted(labels)}")
    return tuple((name, labels[name]) for name in metric.labels)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _sample(name: str, labels: tuple[tuple[str, str], ...], value: float) -> str:
    number = {inf: "+Inf", -inf: "-Inf"}.get(value, repr(value))
    if not labels:
        return f"{name} {number}"
    pairs = ",".join(f'{label}="{_escape(text)}"' for label, text in labels)
    return f"{name}{{{pairs}}} {number}"


class Registry:
    """Current values of the contract metrics, rendered as Prometheus text."""

    def __init__(self) -> None:
        self.values: dict[str, dict[tuple[tuple[str, str], ...], float]] = {m.name: {} for m in CONTRACT}

    def set(self, name: str, value: float, labels: Mapping[str, str] | None = None) -> None:
        """Set a gauge sample."""
        metric = BY_NAME[name]
        if metric.kind != "gauge" or isnan(value):
            raise ValueError(f"{name} takes only a gauge value that is a number")
        self.values[name][_labels(metric, labels or {})] = float(value)

    def inc(self, name: str, value: float = 1.0, labels: Mapping[str, str] | None = None) -> None:
        """Add a non negative amount to a counter sample."""
        metric = BY_NAME[name]
        if metric.kind != "counter" or not isfinite(value) or value < 0:
            raise ValueError(f"{name} takes only non negative counter increments")
        key = _labels(metric, labels or {})
        self.values[name][key] = self.values[name].get(key, 0.0) + float(value)

    def render(self) -> str:
        """Prometheus text exposition: HELP and TYPE for every contract metric, samples only once observed."""
        lines: list[str] = []
        for metric in CONTRACT:
            lines.append(f"# HELP {metric.name} {metric.help}")
            lines.append(f"# TYPE {metric.name} {metric.kind}")
            samples = self.values[metric.name]
            lines.extend(_sample(metric.name, labels, value) for labels, value in sorted(samples.items()))
        return "\n".join(lines) + "\n"


def observe_load(registry: Registry, load: Load) -> None:
    """Set the queue, slot, predicted wait, budget and shed gauges from one load snapshot."""
    free = load.slots_free
    for name in CLASSES:
        labels = {"class": name}
        pending = load.pending.get(name, ())
        wait = predicted_wait(pending, free)
        registry.set("ci_lab_units_queued", len(pending), labels)
        registry.set("ci_lab_units_running", load.running.get(name, 0), labels)
        registry.set("ci_lab_class_shed", 1.0 if name in load.shed else 0.0, labels)
        registry.set("ci_lab_queue_wait_seconds", inf if wait is None else wait, labels)
    registry.set("ci_lab_slots_total", load.slots_total)
    registry.set("ci_lab_slots_busy", load.slots_busy)
    registry.set("ci_lab_budget_fraction", load.budget_fraction)


@dataclass(frozen=True)
class UnitRecord:
    """One finished unit as the scheduler collected it."""

    repo: str
    head: str
    unit_key: str
    kind: str
    priority: str
    worker: str
    conclusion: str
    cached: bool
    seconds: float
    wait_s: float
    ts_ms: int


@dataclass(frozen=True)
class GradeRecord:
    """One graded head as the scheduler posted it."""

    repo: str
    head: str
    base: str
    priority: str
    conclusion: str
    units: int
    cached_units: int
    seconds: float
    ts_ms: int


def _event_id(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def _base(event_id: str, ts_ms: int) -> dict[str, object]:
    return {"event_id": event_id, "ledger": LEDGER, "ts_ms": ts_ms, "plan": "", "phase": "", "slice": "", "task": ""}


def unit_row(unit: UnitRecord) -> dict[str, object]:
    """A lab_units row; one row per unit and head, so a replay replaces it."""
    row = _base(_event_id(unit.repo, unit.head, unit.unit_key), unit.ts_ms)
    row.update(
        repo=unit.repo,
        head=unit.head,
        unit_key=unit.unit_key,
        kind=unit.kind,
        worker=unit.worker,
        conclusion=unit.conclusion,
        cached=int(unit.cached),
        seconds=float(unit.seconds),
        wait_s=float(unit.wait_s),
    )
    row["class"] = unit.priority
    return row


def grade_row(grade: GradeRecord) -> dict[str, object]:
    """A lab_grades row; one row per graded head and class."""
    row = _base(_event_id(grade.repo, grade.head, grade.priority), grade.ts_ms)
    row.update(
        repo=grade.repo,
        head=grade.head,
        base=grade.base,
        conclusion=grade.conclusion,
        units=grade.units,
        cached_units=grade.cached_units,
        seconds=float(grade.seconds),
    )
    row["class"] = grade.priority
    return row


def record(sink: Sink, units: Iterable[UnitRecord], grades: Iterable[GradeRecord]) -> None:
    """Spool unit and grade rows through the metrics outbox."""
    unit_rows = [unit_row(unit) for unit in units]
    grade_rows = [grade_row(grade) for grade in grades]
    if unit_rows:
        sink.append(UNITS, unit_rows)
    if grade_rows:
        sink.append(GRADES, grade_rows)


def observe_records(registry: Registry, units: Iterable[UnitRecord], grades: Iterable[GradeRecord]) -> None:
    """Set unit durations and count verdict cache use and graded heads from collected records."""
    for unit in units:
        registry.set("ci_lab_unit_duration_seconds", unit.seconds, {"kind": unit.kind})
        cache = "ci_lab_cache_hits_total" if unit.cached else "ci_lab_cache_misses_total"
        registry.inc(cache, 1.0, {"cache": "verdict"})
    for grade in grades:
        registry.inc("ci_lab_heads_graded_total", 1.0, {"class": grade.priority, "conclusion": grade.conclusion})


def respond(snapshot: Callable[[], Load], registry: Registry, method: str, target: str) -> tuple[int, str, bytes]:
    """Answer GET /capacity with the capacity document and GET /metrics with the exposition."""
    path = urlsplit(target).path
    if path not in ROUTES:
        return 404, JSON, b'{"error": "not found"}'
    if method != "GET":
        return 405, JSON, b'{"error": "method not allowed"}'
    load = snapshot()
    if path == "/capacity":
        return 200, JSON, json.dumps(document(load)).encode()
    observe_load(registry, load)
    return 200, EXPOSITION, registry.render().encode()


def serve(host: str, port: int, snapshot: Callable[[], Load], registry: Registry) -> ThreadingHTTPServer:
    """Bind the capacity and metrics routes on host and port; the caller runs serve_forever."""

    def answer(request: BaseHTTPRequestHandler) -> None:
        status, content_type, data = respond(snapshot, registry, request.command, request.path)
        request.send_response(status)
        request.send_header("Content-Type", content_type)
        request.send_header("Content-Length", str(len(data)))
        request.end_headers()
        request.wfile.write(data)

    handler = type(BaseHTTPRequestHandler.__name__, (BaseHTTPRequestHandler,), {"do_GET": answer, "do_POST": answer})
    return ThreadingHTTPServer((host, port), handler)
