"""The sending scheduler's index of the run orders it sent, polled each pass to collect the finished hive runs."""

import json
from collections.abc import Callable
from typing import Any

SENT = "run-orders-sent"

Collect = Callable[[Any, str, str, str], bool]


def remember(redis: Any, key: str, hive: str, run_id: str, owner: str) -> None:
    """Record one sent run order so later passes collect its result for its owner."""
    redis.hset(key, run_id, json.dumps({"hive": hive, "owner": owner}))


def poll(redis: Any, key: str, collect: Collect | None = None) -> list[str]:
    """Collect every sent run the hive reported; drop an order once collected, noticed, expired or refused."""
    from scripts.workflows import scheduled

    gather = collect or scheduled.collect
    collected: list[str] = []
    for run_id, raw in redis.hgetall(key).items():
        sent = json.loads(raw)
        receipt = scheduled.receipt(sent["hive"], run_id)
        try:
            done = gather(redis, sent["hive"], run_id, sent["owner"])
        except ValueError:
            redis.hdel(key, run_id)
            continue
        if done:
            collected.append(run_id)
        if done or not redis.exists(receipt) or redis.hexists(receipt, scheduled.NOTICED):
            redis.hdel(key, run_id)
    return collected
