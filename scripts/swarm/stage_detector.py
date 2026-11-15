import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from scripts.swarm import bottleneck, claim_hold, lab_capacity, metrics_outbox, stages
from scripts.swarm.health import findings, standing
from scripts.swarm.store import RedisStore

THRESHOLD_HOURS = {
    "seat_wait": 4.0,
    "review": 4.0,
    "quota_hold": 2.0,
    "host_hold": 2.0,
    "merge_queue": 1.0,
    "intent_fix": 1.0,
    "ci_queue": 1.0,
    "lab_wait": 1.0,
    "ci_overrun": 0.25,
    "launch_failure": 0.25,
}
LAB_UNIT = "minutes behind on pull request grading"
LAB_HELD = "eng claims are held: {hold}"


@dataclass(frozen=True)
class Settings:
    ticks: int = 1
    window_ms: int = bottleneck.WINDOW_MS


def settings(environ: Mapping[str, str] = os.environ) -> Settings:
    ticks = int(environ.get("AGENTIHOOKS_STAGE_DETECTOR_TICKS", "1"))
    hours = float(environ.get("AGENTIHOOKS_STAGE_DETECTOR_WINDOW_HOURS", "4"))
    if ticks < 1 or not 0 < hours <= 12:
        raise ValueError("stage detector needs positive ticks and a window of at most twelve hours")
    return Settings(ticks, int(hours * 3_600_000))


def _totals(found: dict) -> dict:
    totals = {}
    for row in found["stages"]:
        stage = row["stage"]
        total = totals.setdefault(stage, {"stage": stage, "seconds": 0.0, "labels": []})
        total["seconds"] += row["seconds"]
        if row["label"]:
            total["labels"].append(row["label"])
    for total in totals.values():
        total["labels"].sort()
    return totals


def report(
    rows: dict,
    tasks: list,
    now_ms: int,
    window_ms: int = bottleneck.WINDOW_MS,
    ready: Callable[[dict], bool] | None = None,
) -> dict:
    current = stages.report(rows, tasks, now_ms, window_ms, ready)
    previous = _totals(stages.report(rows, tasks, now_ms - window_ms, window_ms, ready))
    totals = _totals(current)
    episodes = stages.observations(rows, tasks, now_ms, window_ms, ready)
    ranked = []
    for stage in totals.keys() | previous.keys():
        row = (
            totals[stage] if stage in totals else {"stage": stage, "seconds": 0.0, "labels": previous[stage]["labels"]}
        )
        before = previous.get(stage, {}).get("seconds", 0.0)
        change = row["seconds"] - before
        ranked.append(
            {
                **row,
                "label": ", ".join(sorted(row["labels"])),
                "previous_seconds": before,
                "trend": "rising" if change > 0 else "falling" if change < 0 else "unchanged",
                "episodes": episodes.get(stage, []),
            }
        )
    ranked.sort(key=lambda row: (-row["seconds"], row["stage"]))
    return {**current, "stages": ranked, "top": ranked[:3]}


def _threshold(row: dict, existing: dict) -> float | None:
    threshold = THRESHOLD_HOURS.get(row["stage"])
    if threshold is None:
        return None
    if row["seconds"] > threshold * 3600 or existing.get("unit", standing.DETECTOR_UNIT) != standing.DETECTOR_UNIT:
        return threshold
    return None


def update_findings(store: RedisStore, slug: str, found: dict, now_ms: int) -> None:
    rows = standing.read(store.redis, slug)
    for row in found["stages"]:
        existing = rows.get(row["stage"], {})
        threshold = _threshold(row, existing)
        if threshold is not None:
            standing.update_detector(store.redis, slug, row, now_ms, threshold)
        elif existing:
            standing.clear_detector(store.redis, slug, row["stage"])
    for stage in rows.keys() - {row["stage"] for row in found["stages"]}:
        standing.clear_detector(store.redis, slug, stage)


def lab_finding(
    store: RedisStore, slug: str, reading: dict[str, Any] | None, limits: findings.Limits, now_ms: int
) -> None:
    """Fire the standing lab wait finding while the reading holds eng claims, and clear it once the reading falls
    back; a lab wait row the detector measured in task hours is left to the detector."""
    minutes = claim_hold.lab_minutes(reading, now_ms)
    row = standing.read(store.redis, slug).get(stages.LAB_WAIT, {})
    mine = row.get("unit") == LAB_UNIT
    if minutes <= limits.ci_behind_minutes:
        if mine:
            standing.clear(store.redis, slug, stages.LAB_WAIT)
        return
    episode = row["episodes"][-1] if mine else f"{stages.LAB_WAIT}:{now_ms}"
    held = LAB_HELD.format(hold=claim_hold.LAB_BEHIND.format(minutes=minutes))
    found = standing.Reading(minutes, limits.ci_behind_minutes, LAB_UNIT, episode, held)
    standing.fire(store.redis, slug, stages.LAB_WAIT, found, now_ms)


def record(
    box: metrics_outbox.Outbox,
    store: RedisStore,
    slug: str,
    now_ms: int,
    tasks: list,
    environ: Mapping[str, str] = os.environ,
    ready: Callable[[dict], bool] | None = None,
) -> dict:
    config = settings(environ)
    lab_finding(store, slug, lab_capacity.status(store, slug), findings.limits(environ), now_ms)
    count = store.redis.incr(store.key(slug, "stage-detector-ticks"))
    if (count - 1) % config.ticks:
        return stages.read(store, slug)
    found = report(stages.read_rows(box, slug, now_ms), tasks, now_ms, config.window_ms, ready)
    update_findings(store, slug, found, now_ms)
    box.append(stages.TABLE, stages.stage_rows(slug, found))
    store.redis.set(store.key(slug, stages.KEY), json.dumps(found))
    return found
