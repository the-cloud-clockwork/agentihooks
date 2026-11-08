from typing import Any

import pytest

from scripts.tick import metrics

pytestmark = pytest.mark.unit


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(metrics.otel, "record_histogram", lambda *args: calls.append(("histogram", *args)))
    monkeypatch.setattr(metrics.otel, "add_counter", lambda *args: calls.append(("counter", *args)))
    monkeypatch.setattr(metrics.otel, "record_gauge", lambda *args: calls.append(("gauge", *args)))
    monkeypatch.setattr(metrics.otel, "flush", lambda *args: calls.append(("flush", *args)))
    return calls


def test_a_successful_step_records_its_duration_by_swarm_step_lane_and_outcome(sent: list[tuple[Any, ...]]) -> None:
    metrics.step_finished("sw", {"step": "rank", "lane": "slow", "outcome": "success", "wall_s": 2.5})
    assert sent == [
        (
            "histogram",
            "agentihooks.swarm.tick.step.duration",
            2.5,
            {"swarm": "sw", "step": "rank", "lane": "slow", "outcome": "success"},
        )
    ]


def test_a_failed_step_without_a_lane_also_counts_one_failure(sent: list[tuple[Any, ...]]) -> None:
    metrics.step_finished("sw", {"step": "launch", "outcome": "error", "wall_s": 1})
    labels = {"swarm": "sw", "step": "launch", "lane": "none", "outcome": "error"}
    assert sent == [
        ("histogram", "agentihooks.swarm.tick.step.duration", 1.0, labels),
        ("counter", "agentihooks.swarm.tick.step.failures", 1, labels),
    ]


def test_a_stopped_step_counts_as_a_failure(sent: list[tuple[Any, ...]]) -> None:
    metrics.step_finished("sw", {"step": "launch", "outcome": "stopped", "wall_s": 0.5})
    assert [call[0] for call in sent] == ["histogram", "counter"]


def test_a_successful_pass_records_duration_and_end_then_flushes(sent: list[tuple[Any, ...]]) -> None:
    metrics.pass_finished("sw", {"outcome": "success", "wall_s": 61.25, "end_ms": 1700000000500})
    assert sent == [
        ("histogram", "agentihooks.swarm.tick.pass.duration", 61.25, {"swarm": "sw", "outcome": "success"}),
        ("gauge", "agentihooks.swarm.tick.pass.end", 1700000000.5, {"swarm": "sw"}),
        ("flush",),
    ]


def test_a_failed_pass_counts_one_failure_before_the_flush(sent: list[tuple[Any, ...]]) -> None:
    metrics.pass_finished("sw", {"outcome": "error", "wall_s": 3, "end_ms": 2000})
    labels = {"swarm": "sw", "outcome": "error"}
    assert sent == [
        ("histogram", "agentihooks.swarm.tick.pass.duration", 3.0, labels),
        ("gauge", "agentihooks.swarm.tick.pass.end", 2.0, {"swarm": "sw"}),
        ("counter", "agentihooks.swarm.tick.pass.failures", 1, labels),
        ("flush",),
    ]
