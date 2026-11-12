"""The HOME resource monitor: registered swarm seats on each hive, masters included, CPU and memory from each live
hive's headroom reading, and running swarms."""

import time
from collections import Counter
from typing import Any

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


def labels(live: list[dict[str, Any]]) -> dict[str, str]:
    """Each live hive's row name by hive id: swarm-master for an interface hive, else its registered name, and its
    member names instead when another live hive shares that name and it has members."""
    base = {hive["id"]: LOCAL if hive.get("ui") == "yes" else hive["name"] for hive in live}
    shared = Counter(base.values())
    named = {hive["id"]: ", ".join(sorted(hive.get("members") or {})) for hive in live}
    return {hive: named[hive] if shared[name] > 1 and named[hive] else name for hive, name in base.items()}


def seats(
    store: object, hives: list[dict[str, Any]], live: list[dict[str, Any]]
) -> list[tuple[str, str, dict[str, int]]]:
    """Registered seats of every swarm, masters included and finished ones left out, by the hive each runs on and the
    harness it records, claude when it records none: one entry per live hive, empty where no seat runs, then one per
    other hive a seat names, under its registered name or, when the registry does not know it, its id."""
    here, host = ports.swarm().commands.hive_id(), ports.hive().reports.host
    names = {hive["id"]: hive["name"] for hive in hives} | labels(live)
    counted = {hive["id"]: dict.fromkeys(HARNESSES, 0) for hive in live}
    for slug in store.slugs():
        for agent in store.agents(slug):
            if agent.state == "finished":
                continue
            counts = counted.setdefault(agent.hive or host(agent) or here, dict.fromkeys(HARNESSES, 0))
            harness = agent.harness or HARNESSES[0]
            counts[harness] = counts.get(harness, 0) + 1
    return [(hive, names.get(hive, hive), counts) for hive, counts in counted.items()]


def tally(source: str, machines: list[tuple[str, str, dict | None]]) -> tuple[dict, list[dict]]:
    """Agent totals by harness and one row per distinct machine key, under its name, with its agents summed across
    every entry sharing that key, None where none of them answered."""
    names = {key: name for key, name, _ in machines}
    merged: dict[str, dict | None] = {}
    for key, _, counts in machines:
        if counts is None:
            merged.setdefault(key, None)
            continue
        current = merged.get(key) or {}
        merged[key] = {harness: current.get(harness, 0) + counts.get(harness, 0) for harness in HARNESSES}
    found = [counts for counts in merged.values() if counts is not None]
    totals = {harness: sum(counts.get(harness, 0) for counts in found) for harness in HARNESSES}
    agents = {"total": sum(totals.values()), "source": source, "reporting": len(found), "of": len(merged), **totals}
    ordered = sorted(merged.items(), key=lambda item: (names[item[0]] != LOCAL, names[item[0]], item[0]))
    rows = [
        {"name": names[key], "agents": None if counts is None else sum(counts.get(harness, 0) for harness in HARNESSES)}
        for key, counts in ordered
    ]
    return agents, rows


def view(store: object, now_ms: int) -> dict:
    registry, headroom = ports.hive().registry, ports.hive().headroom
    running = sum(store.config(slug).state == "running" for slug in store.slugs())
    hives = registry.hives(store.redis)
    live = [hive for hive in hives if registry.live(hive, now_ms)]
    readings = {hive["id"]: headroom.read(store.redis, hive["id"], now_ms) for hive in live}
    measured = [reading for reading in readings.values() if reading is not None]
    agents, rows = tally("seats", seats(store, hives, live))
    used, cores, used_mb, total_mb = (
        (sum(column) for column in zip(*map(capacity, measured))) if measured else (0, 0, 0, 0)
    )
    return {
        "agents": agents,
        "swarms": {"running": running},
        "hives": {"live": len(rows), "measured": len(measured), "rows": rows},
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
