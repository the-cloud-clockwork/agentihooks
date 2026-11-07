"""Swarm tick OpenTelemetry metrics: pass and step durations, failures and when the last pass ended."""

from typing import Any

from hooks.observability import otel

PASS_DURATION = "agentihooks.swarm.tick.pass.duration"
PASS_FAILURES = "agentihooks.swarm.tick.pass.failures"
PASS_END = "agentihooks.swarm.tick.pass.end"
STEP_DURATION = "agentihooks.swarm.tick.step.duration"
STEP_FAILURES = "agentihooks.swarm.tick.step.failures"
SUCCESS = "success"
NO_LANE = "none"
MS_PER_S = 1000


def step_finished(slug: str, record: dict[str, Any]) -> None:
    """Record a finished step's wall time by swarm, step, lane and outcome, and count it when it did not succeed."""
    outcome = str(record["outcome"])
    labels = {"swarm": slug, "step": str(record["step"]), "lane": str(record.get("lane", NO_LANE)), "outcome": outcome}
    otel.record_histogram(STEP_DURATION, float(record["wall_s"]), labels)
    if outcome != SUCCESS:
        otel.add_counter(STEP_FAILURES, 1, labels)


def pass_finished(slug: str, record: dict[str, Any]) -> None:
    """Record a finished pass's wall time and end, count it when it did not succeed, then flush the process's metrics."""
    outcome = str(record["outcome"])
    labels = {"swarm": slug, "outcome": outcome}
    otel.record_histogram(PASS_DURATION, float(record["wall_s"]), labels)
    otel.record_gauge(PASS_END, record["end_ms"] / MS_PER_S, {"swarm": slug})
    if outcome != SUCCESS:
        otel.add_counter(PASS_FAILURES, 1, labels)
    otel.flush()
