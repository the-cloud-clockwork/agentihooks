"""Retires that keep failing: counted per agent each tick, a health finding for the master once they have failed for
two minutes since the first failure."""

import json
from typing import TYPE_CHECKING, Any

from scripts.swarm import elapsed
from scripts.swarm.health.findings import Finding

if TYPE_CHECKING:
    from scripts.swarm.store import AgentRecord, RedisStore

LIMIT_MS = 2 * elapsed.PASS_MS


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
            "a retire failing for 2 minutes",
            row["ticks"],
        )
        for row in waiting
        if elapsed.reached(row["at"] - row["since"], LIMIT_MS)
    ]


def reported_gone(store: Any, agent: "AgentRecord", now_ms: int) -> bool:
    """True when a hive report observed after the launch shows the agent's process gone or no longer lists it."""
    from scripts.hive import reports

    if not agent.hive and not reports.host(agent):
        return False
    hives = reports.Reports(store.redis)
    if hives.gone(agent):
        return True
    raw: str | None = store.redis.get(reports.key(agent.hive)) if agent.hive else None
    launched = agent.launched_at or agent.started_at
    return bool(raw) and json.loads(raw).get("observed_at", 0) >= launched and hives.vanished(agent, now_ms)


def retire_unless_gone(store: "RedisStore", runtime: Any, agent: "AgentRecord", homes: Any, now_ms: int) -> bool:
    """True when the hive report shows the agent gone, else whether the runtime retired it with ``homes``."""
    if reported_gone(store, agent, now_ms):
        return True
    return runtime.retire(agent, homes=homes)
