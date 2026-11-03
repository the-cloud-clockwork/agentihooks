"""OTLP gRPC metric exporter with the delta temporality of the HTTP one, apart because the gRPC exporter is optional."""

from functools import partial

from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter as GrpcExporter  # pyright: ignore

from hooks.observability.otel_delta import TEMPORALITY

GrpcMetricExporter = partial(GrpcExporter, preferred_temporality=TEMPORALITY)  # pyright: ignore
