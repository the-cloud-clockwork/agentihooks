from typing import Any

import pytest

from hooks.observability import otel

pytestmark = pytest.mark.unit


def test_a_histogram_op_creates_one_seconds_histogram_and_records(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[tuple[float, dict[str, str]]] = []
    made: list[tuple[str, str]] = []

    class Histogram:
        def record(self, value: float, attrs: dict[str, str]) -> None:
            recorded.append((value, attrs))

    class Meter:
        def create_histogram(self, name: str, unit: str) -> Histogram:
            made.append((name, unit))
            return Histogram()

    monkeypatch.setattr(otel, "_meter", Meter())
    monkeypatch.setattr(otel, "_histograms", {})
    otel._dispatch_op(("histogram", "h", 1.5, {"swarm": "sw"}))
    otel._dispatch_op(("histogram", "h", 2.0, {"swarm": "sw"}))
    assert recorded == [(1.5, {"swarm": "sw"}), (2.0, {"swarm": "sw"})]
    assert made == [("h", "s")]


def test_a_histogram_op_without_a_meter_counts_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(otel, "_meter", None)
    monkeypatch.setattr(otel, "_counts", {})
    otel._dispatch_op(("histogram", "h", 1.0, {"session.id": "s"}))
    assert otel._counts == {("s", "histograms", "unsupported"): 1}


def test_record_histogram_enqueues_a_histogram_op(monkeypatch: pytest.MonkeyPatch) -> None:
    queued: list[tuple[str, tuple[Any, ...]]] = []
    monkeypatch.setattr(otel, "_enqueue", lambda signal, op: queued.append((signal, op)))
    otel.record_histogram("h", 2, {"swarm": "sw"})
    otel.record_histogram("g", 1)
    assert queued == [
        ("histograms", ("histogram", "h", 2.0, {"swarm": "sw"})),
        ("histograms", ("histogram", "g", 1.0, {})),
    ]


def test_counters_and_histograms_export_as_deltas_so_each_short_process_adds_its_own_share() -> None:
    from opentelemetry.sdk.metrics import Counter, Histogram, UpDownCounter
    from opentelemetry.sdk.metrics.export import AggregationTemporality

    found = otel.temporality()
    assert found == {Counter: AggregationTemporality.DELTA, Histogram: AggregationTemporality.DELTA}
    assert UpDownCounter not in found
