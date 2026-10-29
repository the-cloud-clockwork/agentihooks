"""Hive agents' ledger write results, which open a swarm outage on repeated gateway errors."""

import json
import os
import time

from scripts.swarm import idle

KIND = "hive-ledger"
GATEWAY = (502, 503, 504)
THRESHOLD = 3
WINDOW_MS = 10 * 60 * 1000
KEPT = 20


def _key(redis, slug, name):
    name, prefix = idle._resolved(redis, slug, name)
    return idle.key(slug, "ledger-writes", name, prefix)


def read(redis, slug, name):
    raw = redis.get(_key(redis, slug, name))
    return json.loads(raw) if raw else {}


def record(redis, slug, name, code, now_ms):
    if code is None:
        result = {"ok_at": now_ms}
    else:
        earlier = read(redis, slug, name).get("failed", [])
        result = {"failed": [*earlier, [now_ms, code]][-KEPT:]}
    redis.set(_key(redis, slug, name), json.dumps(result), ex=idle.BEAT_TTL_S)


def report(code, environ=None, redis=None, now_ms=None):
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


def observe(store, slug, now_ms):
    from scripts.swarm import outage

    rows = {agent.name: read(store.redis, slug, agent.name) for agent in store.agents(slug)}
    ok = max((row["ok_at"] for row in rows.values() if "ok_at" in row), default=-1)
    failed = sorted((at, code, name) for name, row in rows.items() for at, code in row.get("failed", []) if at > ok)
    ongoing = any(record["kind"] == KIND for record in outage.open_records(slug))
    fresh = [row for row in failed if row[0] >= now_ms - WINDOW_MS]
    opened = len(fresh) >= THRESHOLD or (ongoing and failed)
    outage.observe(slug, KIND, [_evidence(failed)] if opened else [], now_ms)


def _evidence(failed):
    names = ", ".join(sorted({name for _, _, name in failed}))
    return (
        f"Hive agent ledger writes failed {len(failed)} times with gateway errors since {failed[0][0]}, "
        f"from {names}, last HTTP {failed[-1][1]}"
    )
