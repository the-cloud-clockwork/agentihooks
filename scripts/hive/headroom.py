"""Each hive's measured headroom, published every heartbeat for the workstation tick to admit launches by; numbers
older than two heartbeats count as missing telemetry."""

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.hive import registry
from scripts.swarm import host_budget

if TYPE_CHECKING:
    from redis import Redis

# Two heartbeats of the hive daemon; kept here so readers skip the daemon's imports.
STALE_MS = 30_000
MISSING = "headroom=missing"


def key(hive: str) -> str:
    return f"{registry.key(hive)}:headroom"


def measure(pending: int, now_ms: int, proc: Path = Path("/proc")) -> dict | None:
    try:
        load5 = float((proc / "loadavg").read_text().split()[1])
        available_mb = host_budget._memory_mb(proc)[0]
        total_mb = _total_mb(proc)
    except (OSError, ValueError, IndexError, KeyError, StopIteration):
        return None
    return {
        "cpus": os.cpu_count() or 1,
        "load5": load5,
        "available_mb": available_mb,
        "total_mb": total_mb,
        "pending": pending,
        "measured_at": now_ms,
    }


def _total_mb(proc: Path) -> int:
    line = next(line for line in (proc / "meminfo").read_text().splitlines() if line.startswith("MemTotal:"))
    return int(line.split()[1]) // 1024


def publish(redis: "Redis", hive: str, now_ms: int, proc: Path = Path("/proc")) -> None:
    from scripts.hive.orders import Orders

    measured = measure(redis.xlen(Orders(redis).stream(hive)), now_ms, proc)
    if measured is not None:
        redis.set(key(hive), json.dumps(measured), px=STALE_MS)


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
