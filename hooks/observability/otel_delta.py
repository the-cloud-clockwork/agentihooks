"""OTLP HTTP metric exporter that sends counters and histograms as deltas, so each short process adds only its share."""

from functools import partial

from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter as HttpExporter
from opentelemetry.sdk.metrics import Counter, Histogram
from opentelemetry.sdk.metrics.export import AggregationTemporality

TEMPORALITY = {Counter: AggregationTemporality.DELTA, Histogram: AggregationTemporality.DELTA}
HttpMetricExporter = partial(HttpExporter, preferred_temporality=TEMPORALITY)
