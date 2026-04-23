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

import atexit
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


def init() -> None:
    """Initialize OTEL providers. Call once per hook process.

    No-op if:
      - OTEL SDK is not installed (ImportError)
      - OTEL_HOOKS_ENABLED is false
      - OTEL_EXPORTER_OTLP_ENDPOINT is not set
      - CLAUDE_CODE_ENABLE_TELEMETRY is not set
    """
    global _tracer, _meter, _log_emitter, _initialized
    if _initialized:
        return
    _initialized = True

    if not _can_init():
        return

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

        def _shutdown():
            try:
                tp.force_flush(timeout_millis=500)
            except Exception:
                pass
            tp.shutdown()
            try:
                mp.force_flush(timeout_millis=500)
            except Exception:
                pass
            mp.shutdown()
            try:
                lp.force_flush(timeout_millis=500)
            except Exception:
                pass
            lp.shutdown()

        atexit.register(_shutdown)

    except Exception:
        pass  # OTEL SDK not installed or init failed — all functions remain no-ops


def get_tracer():
    """Return OTEL tracer, or None if unavailable."""
    init()
    return _tracer


def get_meter():
    """Return OTEL meter, or None if unavailable."""
    init()
    return _meter


def emit_event(name: str, attributes: dict[str, str] | None = None) -> None:
    """Emit an OTEL log event (same protocol as Claude Code events).

    Args:
        name: Event name (e.g. "agentihooks.guardrail.secret_detected")
        attributes: Key-value string pairs attached to the event
    """
    init()
    if _log_emitter is None:
        return

    try:
        from opentelemetry._logs import SeverityNumber
        from opentelemetry.sdk._logs import LogRecord

        attrs = dict(attributes or {})
        attrs["event.name"] = name
        attrs["event.timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())

        _log_emitter.emit(
            LogRecord(
                body=name,
                severity_number=SeverityNumber.INFO,
                attributes=attrs,
            )
        )
    except Exception:
        pass  # Never let telemetry failures break hook execution


def record_gauge(name: str, value: float, attributes: dict[str, str] | None = None) -> None:
    """Record a gauge metric value.

    Args:
        name: Metric name (e.g. "agentihooks.tokens.fill_pct")
        value: Current gauge value
        attributes: Optional metric attributes
    """
    init()
    if _meter is None:
        return

    try:
        if name not in _gauges:
            _gauges[name] = _meter.create_gauge(name)
        _gauges[name].set(value, attributes or {})
    except Exception:
        pass
