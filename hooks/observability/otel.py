"""OTEL integration for agentihooks — lightweight, short-lived process safe.

Layer 2 telemetry: custom events and metrics from hook handlers.
Layer 1 (Claude Code native OTEL) uses the same endpoint via env vars.

Design constraints:
  - Hook processes are short-lived (one Python process per event)
  - BatchSpanProcessor / BatchLogRecordProcessor — non-blocking export;
    emit returns immediately, a background thread batches + ships. Short
    hook processes may lose the tail on exit, but hooks MUST NOT block
    on OTel I/O. atexit force_flush(500ms) best-effort catches most.
  - No-op when OTEL SDK not installed or endpoint not configured
  - All providers shut down via atexit with a short flush budget
"""

from __future__ import annotations

import os
import time
from typing import Any

_tracer = None
_meter = None
_log_emitter = None
_initialized = False
_gauges: dict[str, Any] = {}


def _can_init() -> bool:
    """Check if OTEL endpoint is configured and hooks telemetry is enabled."""
    from hooks.config import OTEL_HOOKS_ENABLED

    return (
        OTEL_HOOKS_ENABLED
        and bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"))
        and bool(os.environ.get("CLAUDE_CODE_ENABLE_TELEMETRY"))
    )


# ---------------------------------------------------------------------------
# Defensive worker — all OTEL SDK work happens in a daemon thread so gRPC /
# exporter hangs (C-extension socket blocking) never stall the hook.
# SIGALRM-based timeouts do NOT work here because C code doesn't yield to the
# Python signal handler. Threading is the only reliable isolation.
# ---------------------------------------------------------------------------

import queue as _queue
import threading as _threading

_worker: _threading.Thread | None = None
_q: _queue.Queue | None = None
_init_done = _threading.Event()  # set once _init_sdk finishes (success OR fail)
_worker_lock = _threading.Lock()


def _ensure_worker() -> None:
    """Start the daemon worker thread if not already running."""
    global _worker, _q
    if _worker is not None:
        return
    with _worker_lock:
        if _worker is not None:
            return
        _q = _queue.Queue(maxsize=1000)
        _worker = _threading.Thread(
            target=_worker_loop, name="otel-worker", daemon=True
        )
        _worker.start()


def _worker_loop() -> None:
    """Init OTEL SDK, then drain queue forever. All SDK calls happen here."""
    try:
        if _can_init():
            try:
                _init_sdk()
            except Exception:
                pass
    finally:
        _init_done.set()

    while True:
        try:
            op = _q.get()  # blocks forever waiting for work
        except Exception:
            continue
        try:
            _dispatch_op(op)
        except Exception:
            pass


def _dispatch_op(op: tuple) -> None:
    """Execute a queued OTEL op in the worker thread."""
    kind = op[0]
    if kind == "event":
        _, name, attrs = op
        if _log_emitter is None:
            return
        from opentelemetry._logs import SeverityNumber
        from opentelemetry.sdk._logs import LogRecord

        attrs = dict(attrs)
        attrs["event.name"] = name
        attrs["event.timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        _log_emitter.emit(
            LogRecord(body=name, severity_number=SeverityNumber.INFO, attributes=attrs)
        )
    elif kind == "gauge":
        _, name, value, attrs = op
        if _meter is None:
            return
        if name not in _gauges:
            _gauges[name] = _meter.create_gauge(name)
        _gauges[name].set(value, dict(attrs))


def init() -> None:
    """Initialize OTEL providers. Call once per hook process.

    No-op if:
      - OTEL SDK is not installed (ImportError)
      - OTEL_HOOKS_ENABLED is false
      - OTEL_EXPORTER_OTLP_ENDPOINT is not set
      - CLAUDE_CODE_ENABLE_TELEMETRY is not set
    """
    # init() is now just "ensure worker thread is running". The worker
    # does the actual SDK bootstrap off the main thread so exporter hangs
    # never block the hook.
    global _initialized
    if _initialized:
        return
    _initialized = True
    _ensure_worker()


def _init_sdk() -> None:
    """Actual SDK bootstrap — runs INSIDE the worker thread. Never called directly.

    May block indefinitely on gRPC channel creation if the collector is
    unreachable; that's fine because this is a daemon thread and the main
    hook thread never waits on it.
    """
    global _tracer, _meter, _log_emitter
    try:
        from opentelemetry import metrics, trace
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        from hooks.config import OTEL_HOOKS_SERVICE_NAME

        protocol = os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
        if protocol == "grpc":
            from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        else:
            from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        resource = Resource.create({"service.name": OTEL_HOOKS_SERVICE_NAME})

        # Traces — immediate export (safe for short-lived processes)
        tp = TracerProvider(resource=resource)
        tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(tp)
        _tracer = trace.get_tracer("agentihooks")

        # Langfuse — additional trace exporter (OTLP HTTP only, traces only)
        from hooks.config import (
            OTEL_LANGFUSE_ENABLED,
            OTEL_LANGFUSE_ENDPOINT,
            OTEL_LANGFUSE_PUBLIC_KEY,
            OTEL_LANGFUSE_SECRET_KEY,
        )

        if OTEL_LANGFUSE_ENABLED and OTEL_LANGFUSE_ENDPOINT:
            import base64

            auth = base64.b64encode(f"{OTEL_LANGFUSE_PUBLIC_KEY}:{OTEL_LANGFUSE_SECRET_KEY}".encode()).decode()
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter as LangfuseSpanExporter,
            )

            langfuse_exporter = LangfuseSpanExporter(
                endpoint=f"{OTEL_LANGFUSE_ENDPOINT}/v1/traces",
                headers={
                    "Authorization": f"Basic {auth}",
                    "x-langfuse-ingestion-version": "4",
                },
            )
            tp.add_span_processor(BatchSpanProcessor(langfuse_exporter))

        # Metrics — periodic export, flushed on atexit
        reader = PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=60_000)
        mp = MeterProvider(resource=resource, metric_readers=[reader])
        metrics.set_meter_provider(mp)
        _meter = metrics.get_meter("agentihooks")

        # Logs/Events — immediate export (matches Claude Code's event pattern)
        lp = LoggerProvider(resource=resource)
        lp.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
        _log_emitter = lp.get_logger("agentihooks")

        # No atexit flush — both our worker and OTEL's internal batch threads
        # are daemons; they die with the process. force_flush() honors its
        # timeout arg inconsistently (C-extension blocking) so we skip it
        # entirely rather than risk a hang at process exit.

    except Exception:
        pass  # OTEL SDK not installed or init failed — all functions remain no-ops


def get_tracer():
    """Return OTEL tracer, or None if not yet initialized.

    Blocks up to 50ms waiting for the worker's first init pass. If the
    collector is unreachable and init is still stuck, returns None and
    the caller's `if tracer:` guard keeps us non-blocking.
    """
    init()
    _init_done.wait(timeout=0.05)
    return _tracer


def get_meter():
    """Return OTEL meter, or None if not yet initialized. See get_tracer."""
    init()
    _init_done.wait(timeout=0.05)
    return _meter


def emit_event(name: str, attributes: dict[str, str] | None = None) -> None:
    """Enqueue an OTEL log event for the worker thread. Always non-blocking.

    If the worker hasn't finished init, or the queue is full, the event is
    dropped silently — telemetry loss is acceptable; hook latency is not.
    """
    init()
    if _q is None:
        return
    try:
        _q.put_nowait(("event", name, dict(attributes or {})))
    except _queue.Full:
        pass
    except Exception:
        pass


def record_gauge(name: str, value: float, attributes: dict[str, str] | None = None) -> None:
    """Enqueue a gauge metric for the worker thread. Always non-blocking."""
    init()
    if _q is None:
        return
    try:
        _q.put_nowait(("gauge", name, float(value), dict(attributes or {})))
    except _queue.Full:
        pass
    except Exception:
        pass
