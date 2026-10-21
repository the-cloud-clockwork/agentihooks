"""Each hive's measured headroom, published every heartbeat for the workstation tick to admit launches by; numbers
older than two heartbeats count as missing telemetry."""

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.hive import registry
from scripts.hive.daemon import BEAT_SECONDS, TTL_SECONDS
from scripts.hive.orders import Orders
from scripts.swarm import host_budget

if TYPE_CHECKING:
    from redis import Redis

STALE_MS = 2 * BEAT_SECONDS * 1000
MISSING = "headroom=missing"


def key(hive: str) -> str:
    return f"{registry.key(hive)}:headroom"


def measure(pending: int, now_ms: int, proc: Path = Path("/proc")) -> dict | None:
    try:
        load5 = float((proc / "loadavg").read_text().split()[1])
        available_mb = host_budget._memory_mb(proc)[0]
    except (OSError, ValueError, IndexError, KeyError):
        return None
    return {
        "cpus": os.cpu_count() or 1,
        "load5": load5,
        "available_mb": available_mb,
        "pending": pending,
        "measured_at": now_ms,
    }


def publish(redis: "Redis", hive: str, now_ms: int, proc: Path = Path("/proc")) -> None:
    measured = measure(redis.xlen(Orders(redis).stream(hive)), now_ms, proc)
    if measured is not None:
        redis.set(key(hive), json.dumps(measured), ex=TTL_SECONDS)


def read(redis: "Redis", hive: str, now_ms: int) -> dict | None:
    raw = redis.get(key(hive))
    if raw is None:
        return None
    measured = json.loads(raw)
    return measured if now_ms - measured["measured_at"] <= STALE_MS else None


def summary(measured: dict | None) -> str:
    if measured is None:
        return MISSING
    return (
        f"cpus={measured['cpus']}\tload5={measured['load5']:.2f}\t"
        f"available-mb={measured['available_mb']}\tpending={measured['pending']}"
    )
