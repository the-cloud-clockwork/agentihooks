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
# cgroup v2 mounts the pod's own limits here; on a bare host the root cgroup carries no memory.max or cpu.max.
CGROUP = Path("/sys/fs/cgroup")
MIB = 1024 * 1024


def key(hive: str) -> str:
    return f"{registry.key(hive)}:headroom"


def measure(pending: int, now_ms: int, proc: Path = Path("/proc"), previous: dict | None = None) -> dict | None:
    try:
        load5 = float((proc / "loadavg").read_text().split()[1])
        available_mb = host_budget._memory_mb(proc)[0]
        total_mb = _total_mb(proc)
        pod = _pod(CGROUP, now_ms, previous)
    except (OSError, ValueError, IndexError, KeyError, StopIteration):
        return None
    return {
        "cpus": os.cpu_count() or 1,
        "load5": load5,
        "available_mb": available_mb,
        "total_mb": total_mb,
        **pod,
        "pending": pending,
        "measured_at": now_ms,
    }


def _total_mb(proc: Path) -> int:
    line = next(line for line in (proc / "meminfo").read_text().splitlines() if line.startswith("MemTotal:"))
    return int(line.split()[1]) // 1024


def _pod(cgroup: Path, now_ms: int, previous: dict | None) -> dict:
    pod = {}
    memory = _limit(cgroup / "memory.max")
    if memory is not None:
        total = int(memory[0])
        used = int((cgroup / "memory.current").read_text()) - _stat(cgroup / "memory.stat")["inactive_file"]
        pod["pod_total_mb"] = total // MIB
        pod["pod_available_mb"] = (total - used) // MIB
    cpu = _limit(cgroup / "cpu.max")
    if cpu is not None:
        pod["pod_cpus"] = int(cpu[0]) / int(cpu[1])
        pod["pod_cpu_usec"] = _stat(cgroup / "cpu.stat")["usage_usec"]
        pod.update(_load(pod["pod_cpu_usec"], now_ms, previous))
    return pod


def _load(usage: int, now_ms: int, previous: dict | None) -> dict:
    if previous is None or "pod_cpu_usec" not in previous:
        return {}
    used, elapsed_ms = usage - previous["pod_cpu_usec"], now_ms - previous["measured_at"]
    return {"pod_load": used / (1000 * elapsed_ms)} if used >= 0 and elapsed_ms > 0 else {}


def _stat(path: Path) -> dict[str, int]:
    return {name: int(value) for name, value in (line.split() for line in path.read_text().splitlines())}


def _limit(path: Path) -> list[str] | None:
    if not path.exists():
        return None
    fields = path.read_text().split()
    return None if fields[0] == "max" else fields


def publish(redis: "Redis", hive: str, now_ms: int, proc: Path = Path("/proc")) -> None:
    from scripts.hive.orders import Orders

    measured = measure(redis.xlen(Orders(redis).stream(hive)), now_ms, proc, read(redis, hive, now_ms))
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
