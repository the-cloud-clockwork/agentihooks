"""Hive agents' ledger write results, which open a swarm outage on repeated gateway errors."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING

from scripts.swarm import idle

if TYPE_CHECKING:
    from redis import Redis

    from scripts.swarm.store import RedisStore

KIND = "hive-ledger"
GATEWAY = (502, 503, 504)
THRESHOLD = 3
WINDOW_MS = 10 * 60 * 1000
KEPT = 20


def _key(redis, slug, name):
    name, prefix = idle._resolved(redis, slug, name)
    return idle.key(slug, "ledger-writes", name, prefix)


def read(redis: Redis, slug: str, name: str) -> dict:
    raw = redis.get(_key(redis, slug, name))
    return json.loads(raw) if raw else {}


def record(redis: Redis, slug: str, name: str, code: int | None, now_ms: int) -> None:
    if code is None:
        result = {"ok_at": now_ms}
    else:
        earlier = read(redis, slug, name)
        result = {**earlier, "failed": [*earlier.get("failed", []), [now_ms, code]][-KEPT:]}
    redis.set(_key(redis, slug, name), json.dumps(result), ex=idle.BEAT_TTL_S)


def report(
    code: int | None,
    environ: Mapping[str, str] | None = None,
    redis: Redis | None = None,
    now_ms: int | None = None,
) -> bool:
    from redis.exceptions import RedisError

    from scripts.hive import scope

    env = os.environ if environ is None else environ
    slug, name = env.get("AGENTIHOOKS_SWARM"), env.get("AGENTIHOOKS_AGENT_NAME")
    if not (slug and name and scope.member(env)):
        return False
    try:
        if redis is None:
            from scripts.swarm.store import redis_client

            redis = redis_client(env)
        record(redis, slug, name, code, time.time_ns() // 1_000_000 if now_ms is None else now_ms)
    except (RedisError, OSError):
        return False
    return True


def observe(store: RedisStore, slug: str, now_ms: int) -> None:
    from scripts.swarm import outage

    rows = {agent.name: read(store.redis, slug, agent.name) for agent in store.agents(slug)}
    ok = max((row["ok_at"] for row in rows.values() if "ok_at" in row), default=None)
    failed = sorted(
        (at, code, name) for name, row in rows.items() for at, code in row.get("failed", []) if ok is None or at > ok
    )
    ongoing = [record["started_at"] for record in outage.open_records(slug) if record["kind"] == KIND]
    fresh = [row for row in failed if row[0] >= now_ms - WINDOW_MS]
    opened = len(fresh) >= THRESHOLD or bool(ongoing and failed and failed[0][0] <= ongoing[0])
    outage.observe(slug, KIND, [_evidence(failed)] if opened else [], now_ms)


def _evidence(failed):
    names = ", ".join(sorted({name for _, _, name in failed}))
    return (
        f"Hive agent ledger writes failed {len(failed)} times with gateway errors since {failed[0][0]}, "
        f"from {names}, last HTTP {failed[-1][1]}"
    )
