import pytest
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter as GrpcExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter as HttpExporter
from opentelemetry.sdk.metrics import Counter, Histogram, UpDownCounter
from opentelemetry.sdk.metrics.export import AggregationTemporality

from hooks.observability import otel_delta

pytestmark = pytest.mark.unit


def test_counters_and_histograms_export_as_deltas_so_each_short_process_adds_its_own_share() -> None:
    assert otel_delta.TEMPORALITY == {Counter: AggregationTemporality.DELTA, Histogram: AggregationTemporality.DELTA}
    assert UpDownCounter not in otel_delta.TEMPORALITY


@pytest.mark.parametrize(
    ("exporter", "base"),
    [(otel_delta.GrpcMetricExporter, GrpcExporter), (otel_delta.HttpMetricExporter, HttpExporter)],
)
def test_each_protocol_exporter_carries_delta_temporality(exporter: object, base: type) -> None:
    assert getattr(exporter, "func") is base
    assert getattr(exporter, "keywords") == {"preferred_temporality": otel_delta.TEMPORALITY}


def test_a_built_http_exporter_prefers_deltas_for_counters_and_histograms() -> None:
    built = otel_delta.HttpMetricExporter(endpoint="http://127.0.0.1:9/v1/metrics")
    preferred = built._preferred_temporality or {}
    assert preferred[Counter] is AggregationTemporality.DELTA
    assert preferred[Histogram] is AggregationTemporality.DELTA
    assert preferred[UpDownCounter] is AggregationTemporality.CUMULATIVE
