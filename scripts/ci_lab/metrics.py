"""Prometheus exposition for GET /metrics and the lab_units and lab_grades ClickHouse rows."""

import hashlib
import json
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from math import inf, isfinite, isnan
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from scripts.ci_lab import budget, caches, contract
from scripts.ci_lab.capacity import Load, document, predicted_wait
from scripts.ci_lab.delivery import CLASSES
from scripts.swarm import metrics_outbox

__all__ = [
    "CONTRACT",
    "GRADES",
    "LEDGER",
    "UNITS",
    "GradeRecord",
    "Latest",
    "Metric",
    "Registry",
    "UnitRecord",
    "expose",
    "grade_row",
    "observe_age",
    "observe_load",
    "observe_mirror",
    "observe_rate",
    "observe_unit",
    "record",
    "respond",
    "serve",
    "serve_registry",
    "unit_row",
]

LEDGER = "ci-lab"
JSON = "application/json"
EXPOSITION = "text/plain; version=0.0.4; charset=utf-8"
ROUTES = ("/capacity", "/metrics")
FETCHED_AT = "fetched-at"
RATE_REMAINING = "x-ratelimit-remaining"


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
    contract.LOAD_AGE_SECONDS: "Age of the load snapshot the metrics port serves.",
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
    """Current values of the contract metrics, rendered as Prometheus text; the scheduler's pass thread and the
    metrics port's request threads share one, so every read and write holds its lock."""

    def __init__(self) -> None:
        self.values: dict[str, dict[tuple[tuple[str, str], ...], float]] = {m.name: {} for m in CONTRACT}
        self.lock = threading.Lock()

    def set(self, name: str, value: float, labels: Mapping[str, str] | None = None) -> None:
        """Set a gauge sample."""
        metric = BY_NAME[name]
        if metric.kind != "gauge" or isnan(value):
            raise ValueError(f"{name} takes only a gauge value that is a number")
        key = _labels(metric, labels or {})
        with self.lock:
            self.values[name][key] = float(value)

    def inc(self, name: str, value: float = 1.0, labels: Mapping[str, str] | None = None) -> None:
        """Add a non negative amount to a counter sample."""
        metric = BY_NAME[name]
        if metric.kind != "counter" or not isfinite(value) or value < 0:
            raise ValueError(f"{name} takes only non negative counter increments")
        key = _labels(metric, labels or {})
        with self.lock:
            self.values[name][key] = self.values[name].get(key, 0.0) + float(value)

    def clear(self, name: str) -> None:
        """Drop every sample of a metric, so the exposition holds none for it until the next write."""
        with self.lock:
            self.values[name].clear()

    def render(self) -> str:
        """Prometheus text exposition: HELP and TYPE for every contract metric, samples only once observed."""
        lines: list[str] = []
        with self.lock:
            for metric in CONTRACT:
                lines.append(f"# HELP {metric.name} {metric.help}")
                lines.append(f"# TYPE {metric.name} {metric.kind}")
                samples = self.values[metric.name]
                lines.extend(_sample(metric.name, labels, value) for labels, value in sorted(samples.items()))
        return "\n".join(lines) + "\n"


def observe_load(registry: Registry, load: Load) -> None:
    """Set the queue, slot, predicted wait, budget, shed, wanted slot, ready worker and overdue unit gauges from one
    load snapshot."""
    free = load.slots_free
    for name in CLASSES:
        labels = {"class": name}
        pending = load.pending.get(name, ())
        wait = predicted_wait(pending, free, load.frees_in)
        registry.set("ci_lab_units_queued", len(pending), labels)
        registry.set("ci_lab_units_running", load.running.get(name, 0), labels)
        registry.set("ci_lab_class_shed", 1.0 if name in load.shed else 0.0, labels)
        registry.set("ci_lab_queue_wait_seconds", inf if wait is None else wait, labels)
    registry.set("ci_lab_slots_total", load.slots_total)
    registry.set("ci_lab_slots_busy", load.slots_busy)
    registry.set("ci_lab_budget_fraction", load.budget_fraction)
    registry.set(contract.SLOTS_WANTED, budget.slots_wanted(load.running_slots, load.pending_slots, load.shed))
    registry.set(contract.WORKERS_READY, load.workers_ready)
    registry.set(contract.UNITS_OVERDUE, load.overdue)


def observe_unit(registry: Registry, priority: str, kind: str, slots: int, report: Mapping[str, Any]) -> None:
    """Count a finished attempt's core seconds in its class and the worker cache lookups its report carries, and set
    its kind's last duration when the report measured one."""
    seconds = report.get("seconds")
    registry.inc(contract.CORE_SECONDS_TOTAL, slots * float(seconds or 0.0), {"class": priority})
    if seconds is not None:
        registry.set(contract.UNIT_DURATION_SECONDS, float(seconds), {"kind": kind})
    for cache, (hits, misses) in caches.read(report).items():
        registry.inc(contract.CACHE_HITS_TOTAL, hits, {"cache": cache})
        registry.inc(contract.CACHE_MISSES_TOTAL, misses, {"cache": cache})


def observe_mirror(registry: Registry, mirror: Path, now_ms: int) -> None:
    """Set the mirror age to the seconds since the epoch second the node mirror's fetched-at stamp holds, never
    below zero, and drop the sample while the stamp is missing or holds no whole number."""
    try:
        fetched = int((mirror / FETCHED_AT).read_text())
    except (OSError, ValueError):
        registry.clear(contract.MIRROR_AGE_SECONDS)
        return
    registry.set(contract.MIRROR_AGE_SECONDS, max(0.0, now_ms / 1000 - fetched))


def observe_age(registry: Registry, built_ms: int, now_ms: int) -> None:
    """Set the load age to the seconds since the build of the load snapshot the metrics port serves started, never
    below zero."""
    registry.set(contract.LOAD_AGE_SECONDS, max(0.0, (now_ms - built_ms) / 1000))


def observe_rate(registry: Registry, headers: Mapping[str, str]) -> None:
    """Set the GitHub calls left in the rate window from an answer's x-ratelimit-remaining header when it holds a
    whole number."""
    remaining = headers.get(RATE_REMAINING)
    if remaining is not None and remaining.isdecimal():
        registry.set(contract.GITHUB_RATE_REMAINING, int(remaining))


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


class Latest:
    """The load snapshot the metrics port serves, with the clock reading its build started at. The first read builds
    it and reads that meet that build wait for it; every later read answers at once with the last finished build.
    ``refresh``, which the metrics port's serve loop runs between requests, starts one background build once the last
    build started at least ``refresh_ms`` ago and none runs, so a failed build keeps the last snapshot and the next
    starts an interval after it."""

    def __init__(self, build: Callable[[int], Load], clock: Callable[[], int], refresh_ms: int) -> None:
        self.build = build
        self.clock = clock
        self.refresh_ms = refresh_ms
        self.building = threading.Lock()
        self.started_ms: int | None = None
        self.built: tuple[Load, int] | None = None

    def read(self) -> tuple[Load, int]:
        """The last finished load snapshot and the clock reading its build started at, building the first one."""
        built = self.built
        if built is not None:
            return built
        with self.building:
            return self.built or self._rebuild()

    def refresh(self) -> None:
        """Start one background build once a build has started, the last one started at least ``refresh_ms`` ago and
        none runs."""
        started = self.started_ms
        if started is None or self.clock() - started < self.refresh_ms:
            return
        if self.building.acquire(blocking=False):
            threading.Thread(target=self._background, name="ci-lab-load-refresh", daemon=True).start()

    def _rebuild(self) -> tuple[Load, int]:
        started = self.clock()
        self.started_ms = started
        self.built = (self.build(started), started)
        return self.built

    def _background(self) -> None:
        try:
            self._rebuild()
        finally:
            self.building.release()


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


def _idle() -> None:
    """Run nothing between requests."""


class _Port(ThreadingHTTPServer):
    """A threading HTTP server whose serve loop runs its actions after every request and every poll."""

    def __init__(
        self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], actions: Callable[[], None]
    ) -> None:
        self.actions = actions
        super().__init__(address, handler)

    def service_actions(self) -> None:
        """Run the port's actions."""
        self.actions()


def _bind(
    host: str, port: int, reply: Callable[[str, str], tuple[int, str, bytes]], actions: Callable[[], None] = _idle
) -> ThreadingHTTPServer:
    def answer(request: BaseHTTPRequestHandler) -> None:
        status, content_type, data = reply(request.command, request.path)
        request.send_response(status)
        request.send_header("Content-Type", content_type)
        request.send_header("Content-Length", str(len(data)))
        request.end_headers()
        request.wfile.write(data)

    handler = type(BaseHTTPRequestHandler.__name__, (BaseHTTPRequestHandler,), {"do_GET": answer, "do_POST": answer})
    return _Port((host, port), handler, actions)


def serve(
    host: str, port: int, snapshot: Callable[[], Load], registry: Registry, actions: Callable[[], None] = _idle
) -> ThreadingHTTPServer:
    """Bind the capacity and metrics routes on host and port, running actions between requests; the caller runs
    serve_forever."""
    return _bind(host, port, partial(respond, snapshot, registry), actions)


def expose(registry: Registry, method: str, target: str) -> tuple[int, str, bytes]:
    """Answer GET /metrics with the registry's exposition alone, observing no capacity snapshot."""
    if urlsplit(target).path != "/metrics":
        return 404, JSON, b'{"error": "not found"}'
    if method != "GET":
        return 405, JSON, b'{"error": "method not allowed"}'
    return 200, EXPOSITION, registry.render().encode()


def serve_registry(host: str, port: int, registry: Registry) -> ThreadingHTTPServer:
    """Bind only the metrics route over registry on host and port; the caller runs serve_forever."""
    return _bind(host, port, partial(expose, registry))
