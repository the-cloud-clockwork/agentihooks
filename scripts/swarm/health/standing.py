"""One standing finding per ledger and stage: a new reading updates it in place, a resolve clears it."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from scripts.swarm.health.findings import Finding
from scripts.swarm.store import PREFIX

if TYPE_CHECKING:
    from redis import Redis

KIND = "bottleneck stage"
STALE_MS = 6 * 3_600_000
EPISODES = 20


@dataclass(frozen=True)
class Reading:
    value: float
    threshold: float
    unit: str
    episode: str
    detail: str = ""


def key(slug: str) -> str:
    return f"{PREFIX}:{slug}:standing-findings"


def read(redis: Redis, slug: str) -> dict:
    return {stage: json.loads(raw) for stage, raw in sorted(redis.hgetall(key(slug)).items())}


def _update(redis, slug, stage, transform):
    def apply(pipe):
        raw = pipe.hget(key(slug), stage)
        old = json.loads(raw) if raw else {}
        row = transform(old)
        if row != old:
            pipe.multi()
            pipe.hset(key(slug), stage, json.dumps(row))
        return row

    return redis.transaction(apply, key(slug), value_from_callable=True)


def _fire(old, stage, reading, now_ms):
    episodes = old.get("episodes", [])
    known = {*episodes, *old.get("detector", {}).get("episodes", [])}
    fired = old.get("fired", 0)
    if reading.episode not in known:
        episodes, fired = [*episodes, reading.episode][-EPISODES:], fired + 1
    return {
        **old,
        "stage": stage,
        "value": reading.value,
        "threshold": reading.threshold,
        "unit": reading.unit,
        "detail": reading.detail,
        "fired": fired,
        "episodes": episodes,
        "first_at": old.get("first_at", now_ms),
        "updated_at": now_ms,
    }


def _detector(old, reading, now_ms):
    if old.get("detector") == reading:
        return old
    known = {*old.get("episodes", []), *old.get("detector", {}).get("episodes", [])}
    added = [episode for episode in reading["episodes"] if episode not in known]
    row = {
        "stage": reading["stage"],
        "threshold": 0.0,
        "unit": "task hours",
        "detail": "",
        "first_at": now_ms,
        **old,
        "fired": old.get("fired", 0) + len(added),
        "episodes": [*old.get("episodes", []), *added][-EPISODES:],
        "detector": reading,
        "updated_at": now_ms,
    }
    if row["unit"] == "task hours":
        row["value"] = reading["seconds"] / 3600
    return row


def fire(redis: Redis, slug: str, stage: str, reading: Reading, now_ms: int) -> dict:
    return _update(redis, slug, stage, lambda old: _fire(old, stage, reading, now_ms))


def update_detector(redis: Redis, slug: str, reading: dict, now_ms: int) -> dict:
    return _update(redis, slug, reading["stage"], lambda old: _detector(old, reading, now_ms))


def clear(redis: Redis, slug: str, stage: str) -> bool:
    return bool(redis.hdel(key(slug), stage))


def _finding(row: dict) -> Finding:
    times = "time" if row["fired"] == 1 else "times"
    evidence = (f"Fired {row['fired']} {times}", row["detail"]) if row["detail"] else (f"Fired {row['fired']} {times}",)
    detector = row.get("detector")
    if detector:
        evidence += (
            f"Current {detector['seconds'] / 3600:g} task hours, {detector['trend']},"
            f" previously {detector['previous_seconds'] / 3600:g} task hours",
        )
    return Finding(
        KIND,
        row["stage"],
        f"{row['stage'].replace('_', ' ')} at {row['value']} {row['unit']}, over its {row['threshold']} threshold",
        evidence,
        f"{row['threshold']} {row['unit']}",
        row["fired"],
    )


def findings(redis: Redis, slug: str, now_ms: int) -> list[Finding]:
    return [_finding(row) for row in read(redis, slug).values() if now_ms - row["updated_at"] < STALE_MS]
