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

import fcntl
import hashlib
import os
import time
from pathlib import Path
from typing import Any

_tracer = None
_meter = None
_log_emitter = None
_initialized = False
_gauges: dict[str, Any] = {}
_providers: list = []
FLUSH_TIMEOUT_SEC = 1.0
FLUSH_COOLDOWN_SEC = 60


def _can_init() -> bool:
    """Check if a collector is configured and hooks telemetry is enabled."""
    from hooks.config import OTEL_HOOKS_ENABLED, hook_collector

    return OTEL_HOOKS_ENABLED and bool(hook_collector(os.environ)[0])


def _collector_endpoints() -> tuple[str, dict[str, str]]:
    """Protocol and per-signal exporter endpoint; an HTTP exporter given an endpoint posts to it verbatim."""
    from hooks.config import hook_collector

    endpoint, protocol = hook_collector(os.environ)
    protocol = protocol or "grpc"
    signals = ("traces", "metrics", "logs")
    if protocol == "grpc":
        return protocol, dict.fromkeys(signals, endpoint)
    return protocol, {signal: f"{endpoint.rstrip('/')}/v1/{signal}" for signal in signals}


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
        _worker = _threading.Thread(target=_worker_loop, name="otel-worker", daemon=True)
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
        from opentelemetry._logs import LogRecord, SeverityNumber

        attrs = dict(attrs)
        attrs["event.name"] = name
        attrs["event.timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        _log_emitter.emit(
            LogRecord(timestamp=time.time_ns(), body=name, severity_number=SeverityNumber.INFO, attributes=attrs)
        )
    elif kind == "gauge":
        _, name, value, attrs = op
        if _meter is None:
            return
        if name not in _gauges:
            _gauges[name] = _meter.create_gauge(name)
        _gauges[name].set(value, dict(attrs))
    elif kind == "flush":
        try:
            succeeded = True
            for provider in _providers:
                if provider.force_flush(int(FLUSH_TIMEOUT_SEC * 1000)) is False:
                    succeeded = False
            op[2][0] = succeeded
        finally:
            op[1].set()


def init() -> None:
    """Initialize OTEL providers. Call once per hook process.

    No-op if:
      - OTEL SDK is not installed (ImportError)
      - OTEL_HOOKS_ENABLED is false
      - no collector is set (AGENTIHOOKS_OTLP_ENDPOINT or OTEL_EXPORTER_OTLP_ENDPOINT)
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

        protocol, endpoints = _collector_endpoints()
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
        tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoints["traces"])))
        trace.set_tracer_provider(tp)
        _tracer = trace.get_tracer("agentihooks")

        # Metrics — periodic export, flushed on atexit
        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=endpoints["metrics"]), export_interval_millis=60_000
        )
        mp = MeterProvider(resource=resource, metric_readers=[reader])
        metrics.set_meter_provider(mp)
        _meter = metrics.get_meter("agentihooks")

        # Logs/Events — immediate export (matches Claude Code's event pattern)
        lp = LoggerProvider(resource=resource)
        lp.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(endpoint=endpoints["logs"])))
        _log_emitter = lp.get_logger("agentihooks")
        _providers.extend((tp, mp, lp))

        # No atexit flush — both our worker and OTEL's internal batch threads
        # are daemons; they die with the process. force_flush() honors its
        # timeout arg inconsistently (C-extension blocking) so we skip it
        # entirely rather than risk a hang at process exit.

    except Exception:
        pass  # OTEL SDK not installed or init failed — all functions remain no-ops


LANGFUSE_EXPORT_TIMEOUT_SEC = 10


def langfuse_exporter_config() -> dict | None:
    """Endpoint and headers of the Langfuse trace exporter; None when disabled or a key is missing."""
    import base64

    from hooks import config

    public_key, secret_key = config.OTEL_LANGFUSE_PUBLIC_KEY, config.OTEL_LANGFUSE_SECRET_KEY
    if not (config.OTEL_LANGFUSE_ENABLED and config.OTEL_LANGFUSE_ENDPOINT and public_key and secret_key):
        return None
    auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}", "x-langfuse-ingestion-version": "4"}
    if config.OTEL_LANGFUSE_HOST_HEADER:
        headers["Host"] = config.OTEL_LANGFUSE_HOST_HEADER
    return {"endpoint": f"{config.OTEL_LANGFUSE_ENDPOINT.rstrip('/')}/v1/traces", "headers": headers}


def langfuse_exporter():
    """OTLP HTTP span exporter for Langfuse, or None when it is not configured."""
    settings = langfuse_exporter_config()
    if settings is None:
        return None
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    return OTLPSpanExporter(
        endpoint=settings["endpoint"], headers=settings["headers"], timeout=LANGFUSE_EXPORT_TIMEOUT_SEC
    )


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


def _flush_pending() -> bool:
    done = _threading.Event()
    result = [False]
    try:
        _q.put_nowait(("flush", done, result))
    except _queue.Full:
        return False
    return done.wait(FLUSH_TIMEOUT_SEC) and result[0]


def flush() -> None:
    if _q is None:
        return
    from hooks.config import AGENTIHOOKS_HOME, hook_collector

    endpoint, protocol = hook_collector(os.environ)
    if not endpoint:
        _flush_pending()
        return
    key = hashlib.sha256(f"{endpoint}|{protocol}".encode()).hexdigest()
    state = Path(AGENTIHOOKS_HOME) / "telemetry" / f"flush-{key}"
    try:
        if state.exists() and time.time() < state.stat().st_mtime + FLUSH_COOLDOWN_SEC:
            return
        if _flush_pending():
            state.unlink(missing_ok=True)
            return
        state.parent.mkdir(parents=True, exist_ok=True)
        with state.with_suffix(".lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            if state.exists() and time.time() < state.stat().st_mtime + FLUSH_COOLDOWN_SEC:
                return
            state.touch()
            from hooks.common import log

            log(f"Telemetry flush failed; skipping flush waits for {FLUSH_COOLDOWN_SEC}s")
    except OSError:
        return
