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
DETECTOR_UNIT = "task hours"
SAME_MEASURE = {"task hours in the window": DETECTOR_UNIT}


@dataclass(frozen=True)
class Reading:
    value: float
    threshold: float
    unit: str
    episode: str
    detail: str = ""
    evidence: tuple = ()
    links: tuple = ()


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
        **({"evidence": reading.evidence, "links": reading.links} if reading.links or reading.evidence else {}),
        "fired": fired,
        "episodes": episodes,
        "first_at": old.get("first_at", now_ms),
        "updated_at": now_ms,
    }


def _owned(unit):
    return SAME_MEASURE.get(unit, unit) == DETECTOR_UNIT


def _detector(old, reading, now_ms, threshold):
    if old.get("detector") == reading and (old.get("unit") != DETECTOR_UNIT or old.get("threshold") == threshold):
        return old
    known = {*old.get("episodes", []), *old.get("detector", {}).get("episodes", [])}
    added = [episode for episode in reading["episodes"] if episode not in known]
    unit = old.get("unit", DETECTOR_UNIT)
    if not _owned(unit) and now_ms - old["updated_at"] >= STALE_MS:
        old, unit = {**old, "unit": DETECTOR_UNIT, "detail": ""}, DETECTOR_UNIT
    row = {
        "stage": reading["stage"],
        "unit": DETECTOR_UNIT,
        "detail": "",
        "first_at": now_ms,
        **old,
        "fired": old.get("fired", 0) + len(added),
        "episodes": [*old.get("episodes", []), *added][-EPISODES:],
        "detector": reading,
        "updated_at": now_ms if _owned(unit) else old["updated_at"],
    }
    if row["unit"] == DETECTOR_UNIT:
        row["value"] = reading["seconds"] / 3600
        row["threshold"] = threshold
    return row


def fire(redis: Redis, slug: str, stage: str, reading: Reading, now_ms: int) -> dict:
    return _update(redis, slug, stage, lambda old: _fire(old, stage, reading, now_ms))


def update_detector(redis: Redis, slug: str, reading: dict, now_ms: int, threshold: float) -> dict:
    return _update(redis, slug, reading["stage"], lambda old: _detector(old, reading, now_ms, threshold))


def clear(redis: Redis, slug: str, stage: str) -> bool:
    return bool(redis.hdel(key(slug), stage))


def clear_detector(redis: Redis, slug: str, stage: str) -> bool:
    def apply(pipe):
        raw = pipe.hget(key(slug), stage)
        if not raw or json.loads(raw).get("unit") != DETECTOR_UNIT:
            return False
        pipe.multi()
        pipe.hdel(key(slug), stage)
        return True

    return redis.transaction(apply, key(slug), value_from_callable=True)


def _finding(row: dict) -> Finding:
    times = "time" if row["fired"] == 1 else "times"
    evidence = (f"Fired {row['fired']} {times}", row["detail"]) if row["detail"] else (f"Fired {row['fired']} {times}",)
    evidence += tuple(row.get("evidence", ()))
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
        row["value"],
        SAME_MEASURE.get(row["unit"], row["unit"]),
        links=tuple(row.get("links", ())),
    )


def findings(redis: Redis, slug: str, now_ms: int) -> list[Finding]:
    return [_finding(row) for row in read(redis, slug).values() if now_ms - row["updated_at"] < STALE_MS]
