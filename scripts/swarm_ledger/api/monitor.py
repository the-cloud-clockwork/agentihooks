"""The HOME resource monitor: agent panes the master herdr client publishes for itself and its machines, CPU and memory
from each live hive's headroom reading, and running swarms."""

import json
import time

from redis import RedisError

from scripts.swarm_ledger import ports

from . import resources
from .errors import APIError
from .services import Services

SwarmError = ports.swarm().store.SwarmError
HARNESSES = ("claude", "codex")
LOCAL = "swarm-master"


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


def processes(store: object, hive: str, measured: dict | None) -> dict[str, int] | None:
    """Agent processes on one hive by harness: this host's process table read now, else the one the hive's last
    headroom reading took, else, from a hive that predates that reading, its beat's per-slot session report; None
    when the hive has nothing fresh."""
    if hive == ports.swarm().commands.hive_id():
        found = {harness: len(pids) for harness, pids in ports.hooks().account_sessions.local_processes().items()}
    elif measured is not None and "agents" in measured:
        found = measured["agents"]
    else:
        sessions = store.redis.get(f"{ports.hive().registry.key(hive)}:sessions")
        if sessions is None:
            return None
        found = {}
        for slot, count in json.loads(sessions).items():
            harness = slot.split(":")[0]
            if harness in HARNESSES:
                found[harness] = found.get(harness, 0) + count
    return {harness: int(found.get(harness, 0)) for harness in HARNESSES}


def herdr_machines(redis: object, live: list[dict], now_ms: int) -> list[tuple[str, dict | None]] | None:
    """Each machine the master herdr client on a live interface hive last counted, by name, with None for one that
    did not answer."""
    stats = ports.hive().herdr_stats
    readings = (stats.read(redis, hive["id"], now_ms) for hive in live if hive.get("ui") == "yes")
    published = next((reading for reading in readings if reading is not None), None)
    if published is None:
        return None
    counted = [(LOCAL if name == stats.MASTER else name, counts) for name, counts in published["machines"].items()]
    return counted + [(name, None) for name in published["silent"]]


def hive_machines(store: object, live: list[dict], readings: dict) -> list[tuple[str, dict | None]]:
    return [
        (LOCAL if hive.get("ui") == "yes" else hive["name"], processes(store, hive["id"], readings[hive["id"]]))
        for hive in live
    ]


def tally(source: str, machines: list[tuple[str, dict | None]]) -> tuple[dict, list[dict]]:
    """Agent totals by harness and one row per machine with its agents, None where the machine did not answer."""
    found = [counts for _, counts in machines if counts is not None]
    totals = {harness: sum(counts.get(harness, 0) for counts in found) for harness in HARNESSES}
    agents = {"total": sum(totals.values()), "source": source, "reporting": len(found), "of": len(machines), **totals}
    rows = [
        {"name": name, "agents": None if counts is None else sum(counts.get(harness, 0) for harness in HARNESSES)}
        for name, counts in machines
    ]
    return agents, sorted(rows, key=lambda row: (row["name"] != LOCAL, row["name"]))


def view(store: object, now_ms: int) -> dict:
    registry, headroom = ports.hive().registry, ports.hive().headroom
    running = sum(store.config(slug).state == "running" for slug in store.slugs())
    live = [hive for hive in registry.hives(store.redis) if registry.live(hive, now_ms)]
    readings = {hive["id"]: headroom.read(store.redis, hive["id"], now_ms) for hive in live}
    measured = [reading for reading in readings.values() if reading is not None]
    machines = herdr_machines(store.redis, live, now_ms)
    if machines is None:
        agents, rows = tally("hives", hive_machines(store, live, readings))
    else:
        agents, rows = tally("herdr", machines)
    used, cores, used_mb, total_mb = (
        (sum(column) for column in zip(*map(capacity, measured))) if measured else (0, 0, 0, 0)
    )
    return {
        "agents": agents,
        "swarms": {"running": running},
        "hives": {"live": len(live), "measured": len(measured), "rows": rows},
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
