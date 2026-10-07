"""Retires that keep failing: counted per agent each tick, a health finding for the master from the third tick."""

import json

from scripts.swarm.health.findings import Finding

TICKS = 3


def _key(store, slug):
    return store.key(slug, "retire-failures")


def failed(store, slug, name, refused, now_ms):
    raw = store.redis.hget(_key(store, slug), name)
    row = json.loads(raw) if raw else {"agent": name, "ticks": 0, "since": now_ms}
    row.update(ticks=row["ticks"] + 1, at=now_ms, process=refused["process"], refusal=refused["refusal"])
    store.redis.hset(_key(store, slug), name, json.dumps(row))
    return row["ticks"]


def rows(store, slug):
    return [json.loads(raw) for _, raw in sorted(store.redis.hgetall(_key(store, slug)).items())]


def findings(store, slug):
    return [
        Finding(
            "retire failed",
            row["agent"],
            f"{row['agent']} is still running after {row['ticks']} retire attempts",
            (f"process {row['process']}", f"refusal: {row['refusal']}"),
            f"a retire failing {TICKS} ticks",
            row["ticks"],
        )
        for row in rows(store, slug)
        if row["ticks"] >= TICKS
    ]
