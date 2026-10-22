"""Doctor measure for CI speed findings: the run attempt's own value and its class over a stated window."""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import median_low

from scripts.doctor import ci_speed, ci_speed_read
from scripts.swarm.ledger_events import iso_ms
from scripts.swarm.store import SwarmError

WALL = "ci-tier-wall-time"
RUNNER_WAIT = "ci-runner-wait"
KINDS = (WALL, RUNNER_WAIT, "ci-queue-age-unconfirmed")
WINDOW_BUDGET = (2000, 600)


@dataclass(frozen=True)
class Target:
    kind: str
    run: int | None = None
    attempt: int = 1
    labels: tuple | None = None
    scope: str | None = None


def _grouped(kind: str, scope: str) -> Target | None:
    if kind == WALL:
        return Target(kind, scope=ci_speed.TIERS[scope]) if scope in ci_speed.TIERS else None
    if not scope.startswith("["):
        return None
    labels = json.loads(scope)
    if not all(isinstance(label, str) for label in labels):
        raise ValueError(f"runner labels {scope} are not a list of names")
    return Target(kind, scope=_scope(sorted(set(labels))))


def target(finding: str) -> Target | None:
    kind, _, subject = finding.partition("/")
    if kind not in KINDS:
        return None
    if not subject:
        return Target(kind)
    try:
        head, _, tail = subject.partition("-")
        grouped = _grouped(kind, subject) or (_grouped(kind, tail) if head.isdigit() else None)
        if grouped:
            return grouped
        run, attempt, *rest = subject.split("-", 2)
        labels = tuple(json.loads(rest[0])) if rest and rest[0].startswith("[") else None
        return Target(kind, int(run), int(attempt), labels)
    except ValueError as exc:
        raise SwarmError(f"{finding} names no run attempt, runner label set or tier: {exc}") from exc


def _scope(labels):
    return ", ".join(labels) or "unassigned"


def waits(run: dict, kind: str, now_ms: int) -> dict[tuple, int]:
    longest = {}
    for job in run["jobs"]:
        started = ci_speed.started(job)
        if started != (kind == RUNNER_WAIT):
            continue
        end = job["started_at"] if started else job.get("completed_at")
        labels = tuple(sorted(set(job["labels"])))
        elapsed = (iso_ms(end) if end else now_ms) - iso_ms(job["created_at"])
        longest[labels] = max(longest.get(labels, 0), elapsed)
    return longest


def samples(kind: str, runs: list[dict], now_ms: int) -> dict[str, list[int]]:
    found = {}
    for run in runs:
        if kind == WALL:
            elapsed = ci_speed.wall_ms(run, now_ms)
            if elapsed is not None:
                found.setdefault(ci_speed.TIERS[run["event"]], []).append(elapsed)
            continue
        for labels, elapsed in waits(run, kind, now_ms).items():
            found.setdefault(_scope(labels), []).append(elapsed)
    return found


def value(goal: Target, run: dict, now_ms: int) -> tuple[int, str | None]:
    if goal.kind == WALL:
        elapsed = ci_speed.wall_ms(run, now_ms)
        if elapsed is None:
            raise SwarmError(f"run {goal.run} attempt {goal.attempt} has no tier wall time")
        return elapsed // 1000, ci_speed.TIERS[run["event"]]
    found = waits(run, goal.kind, now_ms)
    if goal.labels is None and found:
        return max(found.values()) // 1000, None
    if goal.labels not in found:
        raise SwarmError(f"run {goal.run} attempt {goal.attempt} has no job on runner labels {json.dumps(goal.labels)}")
    return found[goal.labels] // 1000, _scope(goal.labels)


def _at(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def line(kind: str, scope: str, values: list[int], window: tuple[int, int]) -> str:
    threshold = {WALL: ci_speed.WALL_MS, RUNNER_WAIT: ci_speed.SHORT_WAIT_MS}.get(kind, ci_speed.UNCONFIRMED_WAIT_MS)
    text = (
        f"{kind} {scope}, window {_at(window[0])} to {_at(window[1])}: "
        f"{sum(v > threshold for v in values)} of {len(values)} runs over {threshold // 1000} seconds"
    )
    if not values:
        return text
    return f"{text}, median {median_low(values) // 1000} seconds, worst {max(values) // 1000} seconds"


def measure(finding: str, goal: Target, window: tuple[int, int], now_ms: int) -> Iterator[str]:
    scope = goal.scope
    if goal.run is not None:
        try:
            run = ci_speed_read.attempt(goal.run, goal.attempt)
        except ci_speed_read.FAILURES as exc:
            raise SwarmError(f"run {goal.run} attempt {goal.attempt} unreadable: {type(exc).__name__}: {exc}") from exc
        seconds, scope = value(goal, run, now_ms)
        yield f"{finding} {seconds}"
    snapshot = ci_speed_read.read(*window, active=False, budget=WINDOW_BUDGET)
    if not snapshot["complete"]:
        raise SwarmError(snapshot["error"])
    found = samples(goal.kind, snapshot["runs"], now_ms)
    for name in [scope] if scope is not None else (sorted(found) or ["all"]):
        yield line(goal.kind, name, found.get(name, []), window)
