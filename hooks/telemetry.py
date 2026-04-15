"""Brain telemetry — emit OpenTelemetry spans for brain lifecycle events.

Designed as a thin, fail-silent wrapper around the OTel SDK. If the SDK is
missing, falls back to direct OTLP HTTP POST to /v1/traces. Never blocks the
calling hook, never raises, never logs errors above DEBUG.

Three entry points:
    emit_span(name, attrs)           fire-and-forget span without timing
    span_ctx(name, attrs)            context manager measuring wall duration
    flush()                          drain queued spans (call at process exit)

All functions read config via hooks.config OTEL_* vars. When OTEL_HOOKS_ENABLED
is false they become no-ops.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
import uuid
from contextlib import contextmanager
from typing import Any, Iterator


_SDK_TRACER = None
_SDK_INIT_ATTEMPTED = False
_SDK_LOCK = threading.Lock()


def _try_init_sdk():
    """Initialise the OTel SDK tracer lazily. Safe to call repeatedly."""
    global _SDK_TRACER, _SDK_INIT_ATTEMPTED
    if _SDK_INIT_ATTEMPTED:
        return _SDK_TRACER
    with _SDK_LOCK:
        if _SDK_INIT_ATTEMPTED:
            return _SDK_TRACER
        _SDK_INIT_ATTEMPTED = True
        try:
            from opentelemetry import trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import SimpleSpanProcessor

            from hooks.config import OTEL_HOOKS_SERVICE_NAME

            endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
            if not endpoint:
                return None

            protocol = os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")

            if protocol == "grpc":
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                    OTLPSpanExporter,
                )

                exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
            else:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter,
                )

                traces_endpoint = endpoint.rstrip("/") + "/v1/traces"
                exporter = OTLPSpanExporter(endpoint=traces_endpoint)

            resource = Resource.create({"service.name": OTEL_HOOKS_SERVICE_NAME})
            provider = TracerProvider(resource=resource)
            # SimpleSpanProcessor — blocking export per span. Required for
            # short-lived hook subprocesses that exit before BatchSpanProcessor
            # would flush. Trade-off: higher per-span latency, but spans never
            # get dropped on subprocess exit.
            provider.add_span_processor(SimpleSpanProcessor(exporter))
            trace.set_tracer_provider(provider)
            _SDK_TRACER = trace.get_tracer("agentihooks.brain")
        except Exception:
            _SDK_TRACER = None
        return _SDK_TRACER


def _enabled() -> bool:
    try:
        from hooks.config import OTEL_HOOKS_ENABLED

        return bool(OTEL_HOOKS_ENABLED)
    except Exception:
        return False


def _coerce(attrs: dict[str, Any]) -> dict[str, Any]:
    """Ensure attr values are primitive types OTel accepts."""
    out: dict[str, Any] = {}
    for k, v in attrs.items():
        if v is None:
            continue
        if isinstance(v, (str, int, float, bool)):
            out[k] = v
        else:
            out[k] = str(v)[:500]
    return out


def emit_span(name: str, attrs: dict[str, Any], duration_ms: float | None = None) -> None:
    """Fire-and-forget span emitter. Safe in all conditions."""
    if not _enabled():
        return
    try:
        tracer = _try_init_sdk()
        coerced = _coerce(attrs)
        if tracer is not None:
            start_ns = time.time_ns() - int((duration_ms or 0) * 1_000_000)
            end_ns = time.time_ns() if duration_ms else None
            with tracer.start_as_current_span(
                name,
                start_time=start_ns if duration_ms else None,
                attributes=coerced,
            ) as span:
                if end_ns:
                    span.end(end_time=end_ns)
                    return
        else:
            _http_fallback(name, coerced, duration_ms)
    except Exception:
        pass


class _Span:
    """Minimal span handle returned from span_ctx. Supports set_attrs()."""

    __slots__ = ("_sdk_span", "_attrs")

    def __init__(self, sdk_span=None):
        self._sdk_span = sdk_span
        self._attrs: dict[str, Any] = {}

    def set_attrs(self, attrs: dict[str, Any]) -> None:
        for k, v in _coerce(attrs).items():
            self._attrs[k] = v
            if self._sdk_span is not None:
                try:
                    self._sdk_span.set_attribute(k, v)
                except Exception:
                    pass


@contextmanager
def span_ctx(name: str, attrs: dict[str, Any]) -> Iterator[_Span]:
    """Context manager wrapping a block in a span with wall-clock duration."""
    if not _enabled():
        yield _Span(None)
        return
    tracer = _try_init_sdk()
    start = time.time()
    coerced = _coerce(attrs)
    if tracer is not None:
        try:
            with tracer.start_as_current_span(name, attributes=coerced) as sdk_span:
                handle = _Span(sdk_span)
                yield handle
            return
        except Exception:
            pass
    handle = _Span(None)
    try:
        yield handle
    finally:
        duration_ms = (time.time() - start) * 1000.0
        merged = {**coerced, **handle._attrs}
        _http_fallback(name, merged, duration_ms)


def _http_fallback(name: str, attrs: dict[str, Any], duration_ms: float | None) -> None:
    """Minimal OTLP HTTP JSON trace export. No SDK required."""
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if not endpoint:
        return
    # If configured endpoint is gRPC port (4317), swap to the sibling HTTP port
    # (4318) for the fallback. Prevents silent drops when gRPC SDK isn't available.
    if ":4317" in endpoint:
        endpoint = endpoint.replace(":4317", ":4318")
    url = endpoint.rstrip("/") + "/v1/traces"
    try:
        from hooks.config import OTEL_HOOKS_SERVICE_NAME as svc
    except Exception:
        svc = "agentihooks"

    now_ns = time.time_ns()
    dur_ns = int((duration_ms or 0.1) * 1_000_000)
    start_ns = now_ns - dur_ns
    trace_id = uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]

    payload = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": svc}},
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "agentihooks.brain"},
                        "spans": [
                            {
                                "traceId": trace_id,
                                "spanId": span_id,
                                "name": name,
                                "kind": 1,
                                "startTimeUnixNano": str(start_ns),
                                "endTimeUnixNano": str(now_ns),
                                "attributes": [
                                    {
                                        "key": k,
                                        "value": (
                                            {"stringValue": v}
                                            if isinstance(v, str)
                                            else {"doubleValue": float(v)}
                                            if isinstance(v, (int, float))
                                            else {"boolValue": v}
                                            if isinstance(v, bool)
                                            else {"stringValue": str(v)}
                                        ),
                                    }
                                    for k, v in attrs.items()
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass


def emit_log(message: str, attrs: dict[str, Any]) -> None:
    """Emit an OTLP log record. Used for hook log fan-out (Layer 3).

    Posts a minimal OTLP/HTTP JSON log payload to /v1/logs. Fail-silent.
    """
    if not _enabled():
        return
    if os.getenv("OTEL_HOOK_LOG_FANOUT", "true").lower() != "true":
        return
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if not endpoint or os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "") == "grpc":
        return
    url = endpoint.rstrip("/") + "/v1/logs"
    try:
        from hooks.config import OTEL_HOOKS_SERVICE_NAME as svc
    except Exception:
        svc = "agentihooks"

    now_ns = time.time_ns()
    coerced = _coerce(attrs)
    body_text = message
    if "session_id" in coerced:
        body_text = f"{message} | session={coerced['session_id']}"

    payload = {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": svc}},
                    ]
                },
                "scopeLogs": [
                    {
                        "scope": {"name": "agentihooks.brain.log"},
                        "logRecords": [
                            {
                                "timeUnixNano": str(now_ns),
                                "observedTimeUnixNano": str(now_ns),
                                "severityText": "INFO",
                                "severityNumber": 9,
                                "body": {"stringValue": body_text},
                                "attributes": [
                                    {
                                        "key": k,
                                        "value": (
                                            {"stringValue": v}
                                            if isinstance(v, str)
                                            else {"doubleValue": float(v)}
                                            if isinstance(v, (int, float))
                                            else {"boolValue": v}
                                            if isinstance(v, bool)
                                            else {"stringValue": str(v)}
                                        ),
                                    }
                                    for k, v in coerced.items()
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass


def flush() -> None:
    """Drain queued spans. Safe to call at process exit."""
    if _SDK_TRACER is None:
        return
    try:
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush(timeout_millis=1000)
    except Exception:
        pass
