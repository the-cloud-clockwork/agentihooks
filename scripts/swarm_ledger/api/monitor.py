"""The HOME resource monitor: agents by harness, live hives with their summed headroom, and running swarms."""

import time

from redis import RedisError

from scripts.swarm_ledger import ports

from . import resources
from .errors import APIError
from .services import Services

SwarmError = ports.swarm().store.SwarmError
HARNESSES = ("claude", "codex")


def capacity(measured: dict) -> tuple[float, float, int, int]:
    """Cores in use, cores available, MiB in use and MiB available on one hive, held to its pod limits."""
    cores, used = measured["cpus"], min(measured["load5"], measured["cpus"])
    if "pod_cpus" in measured:
        cores = min(cores, measured["pod_cpus"])
        used = min(measured.get("pod_load", used), cores)
    total, available = measured["total_mb"], measured["available_mb"]
    if "pod_total_mb" in measured and "pod_available_mb" in measured:
        total, available = min(total, measured["pod_total_mb"]), min(available, measured["pod_available_mb"])
    return used, cores, max(total - available, 0), total


def view(store: object, now_ms: int) -> dict:
    registry, headroom = ports.hive().registry, ports.hive().headroom
    agents = dict.fromkeys(HARNESSES, 0)
    running = total = 0
    for slug in store.slugs():
        running += store.config(slug).state == "running"
        for agent in store.agents(slug):
            total += 1
            if agent.harness in agents:
                agents[agent.harness] += 1
    live = [hive for hive in registry.hives(store.redis) if registry.live(hive, now_ms)]
    measured = [m for m in (headroom.read(store.redis, hive["id"], now_ms) for hive in live) if m is not None]
    used, cores, used_mb, total_mb = (
        (sum(column) for column in zip(*map(capacity, measured))) if measured else (0, 0, 0, 0)
    )
    return {
        "agents": {"total": total, **agents},
        "swarms": {"running": running},
        "hives": {"live": len(live), "measured": len(measured)},
        "cpu": {"used": round(used, 1), "cores": round(cores, 1)},
        "memory": {"used_mb": used_mb, "total_mb": total_mb},
    }


def route(services: Services) -> dict:
    try:
        data = view(services.swarm.store(), int(time.time() * 1000))
    except SwarmError as exc:
        raise APIError(503, "storage_error", str(exc)) from None
    except RedisError:
        raise APIError(503, "storage_error", "The swarm store is unreachable") from None
    return {"data": data, "revision": resources.revision(data)}
