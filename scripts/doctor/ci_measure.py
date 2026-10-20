"""Doctor measure for CI speed findings: the run attempt's own value and its class over a stated window."""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import median_low

from scripts.doctor import ci_speed, ci_speed_read
from scripts.swarm.ledger_events import iso_ms
from scripts.swarm.store import SwarmError

WALL = "ci-tier-wall-time"
KINDS = (WALL, "ci-runner-wait", "ci-queue-age-unconfirmed")
WINDOW_BUDGET = (2000, 600)


@dataclass(frozen=True)
class Target:
    kind: str
    run: int | None = None
    attempt: int = 1
    labels: tuple | None = None


def target(finding: str) -> Target | None:
    kind, _, subject = finding.partition("/")
    if kind not in KINDS:
        return None
    if not subject:
        return Target(kind)
    try:
        run, attempt, *rest = subject.split("-", 2)
        labels = tuple(json.loads(rest[0])) if rest and rest[0].startswith("[") else None
        return Target(kind, int(run), int(attempt), labels)
    except ValueError as exc:
        raise SwarmError(f"{finding} names no run attempt: {exc}") from exc


def _scope(labels):
    return ", ".join(labels) or "unassigned"


def waits(run: dict, now_ms: int) -> dict[tuple, int]:
    longest = {}
    for job in run["jobs"]:
        began = iso_ms(job["started_at"]) if ci_speed.started(job) else now_ms
        labels = tuple(sorted(set(job["labels"])))
        longest[labels] = max(longest.get(labels, 0), (began - iso_ms(job["created_at"])) // 1000)
    return longest


def samples(kind: str, runs: list[dict], now_ms: int) -> dict[str, list[int]]:
    found = {}
    for run in runs:
        if kind == WALL:
            elapsed = ci_speed.wall_ms(run, now_ms)
            if elapsed is not None:
                found.setdefault(ci_speed.TIERS[run["event"]], []).append(elapsed // 1000)
            continue
        for labels, seconds in waits(run, now_ms).items():
            found.setdefault(_scope(labels), []).append(seconds)
    return found


def value(goal: Target, run: dict, now_ms: int) -> tuple[int, str | None]:
    if goal.kind == WALL:
        elapsed = ci_speed.wall_ms(run, now_ms)
        if elapsed is None:
            raise SwarmError(f"run {goal.run} attempt {goal.attempt} has no tier wall time")
        return elapsed // 1000, ci_speed.TIERS[run["event"]]
    found = waits(run, now_ms)
    if goal.labels is None and found:
        return max(found.values()), None
    if goal.labels not in found:
        raise SwarmError(f"run {goal.run} attempt {goal.attempt} has no job on runner labels {json.dumps(goal.labels)}")
    return found[goal.labels], _scope(goal.labels)


def _at(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def line(kind: str, scope: str, values: list[int], window: tuple[int, int]) -> str:
    threshold = (ci_speed.WALL_MS if kind == WALL else ci_speed.SHORT_WAIT_MS) // 1000
    text = (
        f"{kind} {scope}, window {_at(window[0])} to {_at(window[1])}: "
        f"{sum(v > threshold for v in values)} of {len(values)} runs over {threshold} seconds"
    )
    return f"{text}, median {median_low(values)} seconds, worst {max(values)} seconds" if values else text


def measure(finding: str, goal: Target, window: tuple[int, int], now_ms: int) -> list[str]:
    lines, scope = [], None
    if goal.run is not None:
        try:
            run = ci_speed_read.attempt(goal.run, goal.attempt)
        except ci_speed_read.FAILURES as exc:
            raise SwarmError(f"run {goal.run} attempt {goal.attempt} unreadable: {type(exc).__name__}: {exc}") from exc
        seconds, scope = value(goal, run, now_ms)
        lines.append(f"{finding} {seconds}")
    snapshot = ci_speed_read.read(*window, active=False, budget=WINDOW_BUDGET)
    if not snapshot["complete"]:
        raise SwarmError(snapshot["error"])
    found = samples(goal.kind, snapshot["runs"], now_ms)
    scopes = [scope] if scope is not None else sorted(found)
    return lines + [line(goal.kind, name, found.get(name, []), window) for name in scopes]
