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


def fire(redis: Redis, slug: str, stage: str, reading: Reading, now_ms: int) -> dict:
    raw = redis.hget(key(slug), stage)
    old = json.loads(raw) if raw else {}
    episodes = old.get("episodes", [])
    fired = old.get("fired", 0)
    if reading.episode not in episodes:
        episodes, fired = [*episodes, reading.episode][-EPISODES:], fired + 1
    row = {
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
    redis.hset(key(slug), stage, json.dumps(row))
    return row


def clear(redis: Redis, slug: str, stage: str) -> bool:
    return bool(redis.hdel(key(slug), stage))


def _finding(row: dict) -> Finding:
    times = "time" if row["fired"] == 1 else "times"
    evidence = (f"Fired {row['fired']} {times}", row["detail"]) if row["detail"] else (f"Fired {row['fired']} {times}",)
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
