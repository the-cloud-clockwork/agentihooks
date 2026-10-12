import json
import os
from collections.abc import Mapping
from dataclasses import dataclass

from scripts.swarm import bottleneck, metrics_outbox, stages
from scripts.swarm.health import standing
from scripts.swarm.store import RedisStore

THRESHOLD_HOURS = {
    "claim_wait": 4.0,
    "review": 4.0,
    "quota_hold": 2.0,
    "host_hold": 2.0,
    "merge_queue": 1.0,
    "intent_fix": 1.0,
    "ci_queue": 1.0,
    "ci_overrun": 0.25,
    "launch_failure": 0.25,
}


@dataclass(frozen=True)
class Settings:
    ticks: int = 5
    window_ms: int = bottleneck.WINDOW_MS


def settings(environ: Mapping[str, str] = os.environ) -> Settings:
    ticks = int(environ.get("AGENTIHOOKS_STAGE_DETECTOR_TICKS", "5"))
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


def report(rows: dict, tasks: list, now_ms: int, window_ms: int = bottleneck.WINDOW_MS) -> dict:
    current = stages.report(rows, tasks, now_ms, window_ms)
    previous = _totals(stages.report(rows, tasks, now_ms - window_ms, window_ms))
    totals = _totals(current)
    episodes = stages.observations(rows, tasks, now_ms, window_ms)
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


def record(
    box: metrics_outbox.Outbox,
    store: RedisStore,
    slug: str,
    now_ms: int,
    tasks: list,
    environ: Mapping[str, str] = os.environ,
) -> dict:
    config = settings(environ)
    count = store.redis.incr(store.key(slug, "stage-detector-ticks"))
    if (count - 1) % config.ticks:
        return stages.read(store, slug)
    found = report(stages.read_rows(box, slug, now_ms), tasks, now_ms, config.window_ms)
    update_findings(store, slug, found, now_ms)
    box.append(stages.TABLE, stages.stage_rows(slug, found))
    store.redis.set(store.key(slug, stages.KEY), json.dumps(found))
    return found
