import json
import math
import time
from collections import Counter
from typing import Any

HOUR_MS = 3_600_000


def record(store: Any, slug: str, harness: str | None, name: str | None, launched_at: int) -> None:
    if launched_at <= 0 or not harness or not name:
        return
    key = store.key(slug, "spawned")
    member = json.dumps([harness, name, launched_at])
    pipe = store.redis.pipeline()
    pipe.zadd(key, {member: launched_at})
    pipe.zremrangebyscore(key, -math.inf, f"({int(time.time() * 1000) - HOUR_MS}")
    pipe.execute()


def read(store: Any, slug: str) -> dict[str, int]:
    now = int(time.time() * 1000)
    members: list[str] = store.redis.zrangebyscore(store.key(slug, "spawned"), now - HOUR_MS, now)
    harnesses: list[str] = [json.loads(member)[0] for member in members]
    return dict(Counter(harnesses))


def completed(store: Any, slug: str, agent: Any, runtime: Any) -> None:
    retire = runtime.retire
    try:
        record(store, slug, agent.harness, agent.name, agent.launched_at)
    except Exception:
        retire(agent)
        raise


def placed(store: Any, slug: str, agent: Any, runtime: Any) -> None:
    completed(store, slug, agent, runtime)
    store.put_agent(slug, agent)
