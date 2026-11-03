"""OTLP metric exporters that send counters and histograms as deltas, so each short lived process adds only its share."""

from functools import partial

from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter as GrpcExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter as HttpExporter
from opentelemetry.sdk.metrics import Counter, Histogram
from opentelemetry.sdk.metrics.export import AggregationTemporality

TEMPORALITY = {Counter: AggregationTemporality.DELTA, Histogram: AggregationTemporality.DELTA}
GrpcMetricExporter = partial(GrpcExporter, preferred_temporality=TEMPORALITY)
HttpMetricExporter = partial(HttpExporter, preferred_temporality=TEMPORALITY)
