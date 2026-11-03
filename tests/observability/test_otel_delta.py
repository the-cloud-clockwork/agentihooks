import pytest
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter as HttpExporter
from opentelemetry.sdk.metrics import Counter, Histogram, UpDownCounter
from opentelemetry.sdk.metrics.export import AggregationTemporality

from hooks.observability import otel_delta

pytestmark = pytest.mark.unit


def test_counters_and_histograms_export_as_deltas_so_each_short_process_adds_its_own_share() -> None:
    assert otel_delta.TEMPORALITY == {Counter: AggregationTemporality.DELTA, Histogram: AggregationTemporality.DELTA}
    assert UpDownCounter not in otel_delta.TEMPORALITY


def test_the_http_exporter_carries_delta_temporality() -> None:
    assert otel_delta.HttpMetricExporter.func is HttpExporter
    assert otel_delta.HttpMetricExporter.keywords == {"preferred_temporality": otel_delta.TEMPORALITY}
    built = otel_delta.HttpMetricExporter(endpoint="http://127.0.0.1:9/v1/metrics")
    preferred = built._preferred_temporality or {}
    assert preferred[Counter] is AggregationTemporality.DELTA
    assert preferred[Histogram] is AggregationTemporality.DELTA
    assert preferred[UpDownCounter] is AggregationTemporality.CUMULATIVE
