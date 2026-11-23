from __future__ import annotations

import json
import sys
import threading
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

pytestmark = pytest.mark.unit


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[InMemorySpanExporter, list[TracerProvider], Mock]]:
    from opentelemetry import metrics, trace
    from opentelemetry.sdk import _logs
    from opentelemetry.sdk import trace as trace_sdk
    from opentelemetry.sdk._logs import export as log_export
    from opentelemetry.sdk.metrics import export as metric_export
    from opentelemetry.util._once import Once

    from hooks import config, telemetry
    from hooks.observability import otel

    exporter = InMemorySpanExporter()
    providers = []
    register = Mock(wraps=trace.set_tracer_provider)
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", None)
    monkeypatch.setattr(trace, "_TRACER_PROVIDER_SET_ONCE", Once())
    monkeypatch.setattr(trace, "set_tracer_provider", register)
    monkeypatch.setattr(config, "OTEL_HOOKS_ENABLED", True)
    monkeypatch.setenv("AGENTIHOOKS_OTLP_ENDPOINT", "http://127.0.0.1:4318")
    monkeypatch.setenv("AGENTIHOOKS_OTLP_PROTOCOL", "http/protobuf")
    monkeypatch.setattr(telemetry, "_SDK_TRACER", None, raising=False)
    monkeypatch.setattr(telemetry, "_SDK_INIT_ATTEMPTED", False, raising=False)
    for name in ("_tracer", "_meter", "_log_emitter", "_q"):
        monkeypatch.setattr(otel, name, None)
    monkeypatch.setattr(otel, "_initialized", False)
    monkeypatch.setattr(otel, "_init_done", threading.Event())
    monkeypatch.setattr(otel, "_providers", [])
    monkeypatch.setattr(otel, "_counts", {})

    def provider(*args: Any, **kwargs: Any) -> TracerProvider:
        result = TracerProvider(*args, **kwargs)
        providers.append(result)
        return result

    def batch(span_exporter: SpanExporter) -> BatchSpanProcessor:
        return BatchSpanProcessor(span_exporter, schedule_delay_millis=600_000)

    def worker() -> None:
        monkeypatch.setattr(otel, "_q", Mock(put_nowait=Mock(side_effect=otel._dispatch_op)))
        otel._init_sdk()
        otel._init_done.set()

    monkeypatch.setattr(trace_sdk, "TracerProvider", provider)
    monkeypatch.setattr("opentelemetry.sdk.trace.export.BatchSpanProcessor", batch)
    monkeypatch.setattr(
        "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter", Mock(return_value=exporter)
    )
    monkeypatch.setattr("opentelemetry.sdk.metrics.MeterProvider", Mock())
    monkeypatch.setattr(metric_export, "PeriodicExportingMetricReader", Mock())
    monkeypatch.setattr("hooks.observability.otel_delta.HttpMetricExporter", Mock())
    monkeypatch.setattr(metrics, "set_meter_provider", Mock())
    monkeypatch.setattr(metrics, "get_meter", Mock())
    monkeypatch.setattr(_logs, "LoggerProvider", Mock())
    monkeypatch.setattr(log_export, "BatchLogRecordProcessor", Mock())
    monkeypatch.setattr("opentelemetry.exporter.otlp.proto.http._log_exporter.OTLPLogExporter", Mock())
    monkeypatch.setattr(otel, "_ensure_worker", worker)
    try:
        yield exporter, providers, register
    finally:
        for item in providers:
            item.shutdown()


@pytest.mark.feature("hooks-brain-shared-tracer-provider")
@pytest.mark.parametrize("brain_first", [False, True], ids=["hook-first", "brain-first"])
def test_hook_and_brain_spans_share_one_tracer_provider(
    sdk: tuple[InMemorySpanExporter, list[TracerProvider], Mock], brain_first: bool
) -> None:
    from hooks import telemetry
    from hooks.observability import otel

    exporter, providers, register = sdk
    if brain_first:
        telemetry.emit_span("brain.delivery", {"brain": True})
    tracer = otel.get_tracer()
    assert tracer is not None
    with tracer.start_as_current_span("hook.dispatch", attributes={"hook": True}):
        pass
    if not brain_first:
        telemetry.emit_span("brain.delivery", {"brain": True})
    telemetry.flush()
    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert set(spans) == {"hook.dispatch", "brain.delivery"}
    assert spans["hook.dispatch"].instrumentation_scope.name == "agentihooks"
    assert spans["brain.delivery"].instrumentation_scope.name == "agentihooks.brain"
    assert len(providers) == 1
    assert register.call_count == 1


@pytest.mark.feature("hooks-brain-shared-tracer-provider")
def test_emit_span_preserves_name_attributes_and_times(
    sdk: tuple[InMemorySpanExporter, list[TracerProvider], Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    from hooks import telemetry

    monkeypatch.setattr(telemetry, "time", SimpleNamespace(time_ns=Mock(side_effect=[1_000_000_000, 2_000_000_000])))
    telemetry.emit_span(
        "brain.ingest",
        {"text": "value", "count": 2, "ratio": 0.5, "ready": True, "skip": None, "items": [1], "long": "x" * 600},
        duration_ms=12.5,
    )
    telemetry.flush()
    (span,) = sdk[0].get_finished_spans()
    assert span.name == "brain.ingest"
    assert dict(span.attributes) == {
        "text": "value",
        "count": 2,
        "ratio": 0.5,
        "ready": True,
        "items": "[1]",
        "long": "x" * 500,
    }
    assert span.start_time == 987_500_000
    assert span.end_time == 2_000_000_000
    assert span.instrumentation_scope.name == "agentihooks.brain"


@pytest.mark.feature("hooks-brain-shared-tracer-provider")
def test_span_ctx_exports_attributes_and_updated_attributes(
    sdk: tuple[InMemorySpanExporter, list[TracerProvider], Mock],
) -> None:
    from hooks import telemetry

    with telemetry.span_ctx("brain.tick", {"initial": 1, "skip": None}) as handle:
        handle.set_attrs({"initial": 2, "items": [3], "skip": None})
    telemetry.flush()
    (span,) = sdk[0].get_finished_spans()
    assert span.name == "brain.tick"
    assert dict(span.attributes) == {"initial": 2, "items": "[3]"}
    assert span.instrumentation_scope.name == "agentihooks.brain"
    assert span.start_time <= span.end_time


@pytest.mark.feature("hooks-brain-shared-tracer-provider")
def test_telemetry_without_spans_never_starts_the_sdk(
    sdk: tuple[InMemorySpanExporter, list[TracerProvider], Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    from hooks import config, telemetry
    from hooks.observability import otel

    telemetry.flush()
    monkeypatch.setattr(config, "OTEL_HOOKS_ENABLED", False)
    telemetry.emit_span("disabled", {})
    with telemetry.span_ctx("disabled", {}) as handle:
        handle.set_attrs({"value": 1})
    telemetry.flush()
    assert sdk[1] == []
    sdk[2].assert_not_called()
    assert otel._initialized is False
    assert otel._q is None


@pytest.mark.feature("hooks-brain-shared-tracer-provider")
def test_telemetry_flush_drains_the_shared_otel_providers(
    sdk: tuple[InMemorySpanExporter, list[TracerProvider], Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    from hooks import telemetry
    from hooks.observability import otel

    flush = Mock(wraps=otel.flush)
    monkeypatch.setattr(otel, "flush", flush)
    telemetry.emit_span("brain.flush", {"value": 1})
    telemetry.flush()
    flush.assert_called_once_with()
    (span,) = sdk[0].get_finished_spans()
    assert span.name == "brain.flush"
    assert dict(span.attributes) == {"value": 1}


@pytest.mark.feature("hooks-brain-shared-tracer-provider")
def test_sdk_unavailable_keeps_direct_http_span_fallback(
    sdk: tuple[InMemorySpanExporter, list[TracerProvider], Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    from hooks import telemetry

    monkeypatch.setitem(sys.modules, "opentelemetry.sdk.trace", None)
    post = Mock()
    monkeypatch.setattr(telemetry.urllib.request, "urlopen", post)
    monkeypatch.setattr(
        telemetry, "time", SimpleNamespace(time_ns=lambda: 1_000_000_000, time=Mock(side_effect=[10.0, 10.25]))
    )
    telemetry.emit_span("brain.fallback", {"value": "kept", "skip": None}, duration_ms=12.5)
    with telemetry.span_ctx("brain.context.fallback", {"initial": "kept"}) as handle:
        handle.set_attrs({"extra": [1]})
    assert post.call_count == 2
    spans = []
    for call in post.call_args_list:
        request = call.args[0]
        assert request.full_url == "http://127.0.0.1:4318/v1/traces"
        assert call.kwargs == {"timeout": 2}
        (scope,) = json.loads(request.data)["resourceSpans"][0]["scopeSpans"]
        assert scope["scope"] == {"name": "agentihooks.brain"}
        spans.extend(scope["spans"])
    assert [span["name"] for span in spans] == ["brain.fallback", "brain.context.fallback"]
    assert [span["startTimeUnixNano"] for span in spans] == ["987500000", "750000000"]
    assert [span["endTimeUnixNano"] for span in spans] == ["1000000000", "1000000000"]
    assert spans[0]["attributes"] == [{"key": "value", "value": {"stringValue": "kept"}}]
    assert spans[1]["attributes"] == [
        {"key": "initial", "value": {"stringValue": "kept"}},
        {"key": "extra", "value": {"stringValue": "[1]"}},
    ]
    assert sdk[1] == []
