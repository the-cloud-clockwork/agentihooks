from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

from scripts.swarm.health import standing
from scripts.swarm.keyspace import ROOT
from scripts.swarm.langfuse_config import Limits
from scripts.swarm.langfuse_read import read_summary

if TYPE_CHECKING:
    from redis import Redis

    from scripts.swarm.store import AgentRecord, RedisStore

PREFIX = "langfuse_account_"


def breaches(row: dict, limits: Limits) -> list[tuple[str, float, float, str]]:
    if row["observations"] >= limits.min_observations and row["error_rate"] > limits.error_rate:
        return [("error rate", row["error_rate"], limits.error_rate, "fraction")]
    return []


def apply(redis: Redis, slug: str, summary: dict, bindings: list[AgentRecord], now_ms: int, limits: Limits) -> int:
    if summary["swarm"] != slug:
        raise ValueError("Langfuse summary belongs to another swarm")
    identities = {agent.name: agent for agent in bindings}
    grouped = defaultdict(list)
    for row in summary["agents"]:
        agent = identities.get(row["agent"])
        stages = breaches(row, limits)
        if agent and agent.account and stages:
            grouped[agent.account].extend((agent, stage) for stage in stages)
    window = summary["window"]
    episode = window["toTimestamp"]
    for account, stages in grouped.items():
        detail = [f"account {account}; window {window['fromTimestamp']} to {episode}"]
        for agent, (stage, value, threshold, unit) in stages:
            detail.append(f"agent {agent.name}; task {agent.task}; {stage} {value} {unit}, threshold {threshold}")
        ratio = max(value / threshold for _, (_, value, threshold, _) in stages)
        standing.fire(
            redis,
            slug,
            PREFIX + account,
            standing.Reading(ratio, 1, "threshold ratio", episode, "; ".join(detail)),
            now_ms,
        )
    for stage in standing.read(redis, slug):
        if stage.startswith(PREFIX) and stage.removeprefix(PREFIX) not in grouped:
            standing.clear(redis, slug, stage)
    return len(grouped)


def refresh(
    store: RedisStore,
    slug: str,
    now_ms: int,
    read: Callable[[str, dict, float], dict] = read_summary,
    limits: Limits = Limits(),
) -> list[str]:
    key = f"{ROOT}:swarm:{slug}:langfuse-summary"
    raw = store.redis.get(key)
    cached = json.loads(raw) if raw else {}
    if not os.environ.get("LANGFUSE_MCP_URL"):
        if cached.get("disabled"):
            return []
        store.redis.set(key, json.dumps({**cached, "disabled": True}))
        return ["Langfuse sampling is off: configure LANGFUSE_MCP_URL"]
    if cached.get("disabled"):
        cached = {}
    if "tried_at" in cached and now_ms - cached["tried_at"] < limits.interval_ms:
        return []
    window = {
        "fromTimestamp": datetime.fromtimestamp((now_ms - limits.window_ms) / 1000, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "toTimestamp": datetime.fromtimestamp(now_ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    record = {**cached, "tried_at": now_ms}
    try:
        sample = read(slug, window, limits.read_timeout_seconds)
        count = apply(store.redis, slug, sample, store.execution_registry.records(slug), now_ms, limits)
    except Exception as exc:
        record["error"] = type(exc).__name__
        store.redis.set(key, json.dumps(record))
        return []
    store.redis.set(key, json.dumps({"tried_at": now_ms, "at": now_ms, "accounts": count}))
    return [f"Langfuse summary read: {count} breaching accounts"]
