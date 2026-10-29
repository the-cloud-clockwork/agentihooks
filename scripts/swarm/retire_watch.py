"""Retires that keep failing: counted per agent each tick, a health finding for the master from the third tick."""

import json
from typing import TYPE_CHECKING

from scripts.swarm.health.findings import Finding

if TYPE_CHECKING:
    from scripts.swarm.store import RedisStore

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


def _evidence(row, host, indexed, absent):
    evidence = (f"process {row['process']}", f"refusal: {row['refusal']}")
    if not host:
        return evidence
    return evidence + (
        f"hive {host}",
        "hive indexed" if host in indexed else "hive absent from index",
        f"retire waits on absent hives {absent}",
    )


def findings(store: "RedisStore", slug: str) -> list[Finding]:
    from scripts.hive import registry, reports

    waiting = rows(store, slug)
    hosts = {agent.name: host for agent in store.agents(slug) if (host := reports.host(agent))}
    indexed = registry.hive_state.index(store.redis)
    absent = sum(row["agent"] in hosts and hosts[row["agent"]] not in indexed for row in waiting)
    return [
        Finding(
            "retire failed",
            row["agent"],
            f"{row['agent']} is still running after {row['ticks']} retire attempts",
            _evidence(row, hosts.get(row["agent"]), indexed, absent),
            f"a retire failing {TICKS} ticks",
            row["ticks"],
        )
        for row in waiting
        if row["ticks"] >= TICKS
    ]
