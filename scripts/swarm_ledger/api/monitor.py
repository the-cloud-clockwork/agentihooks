"""The HOME resource monitor: agent panes from the main herdr server and every machine plugged into it, CPU and memory
from each live hive's headroom reading, and running swarms."""

import json
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from redis import RedisError

from scripts.swarm_ledger import ports

from . import resources
from .errors import APIError
from .services import Services

SwarmError = ports.swarm().store.SwarmError
HARNESSES = ("claude", "codex")
HERDR_TIMEOUT_S = 8
PANES_FRESH_S = 10
_panes: dict = {}
_panes_lock = threading.Lock()


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


def herdr(binary: str, *args: str) -> object | None:
    try:
        done = subprocess.run([binary, *args], capture_output=True, text=True, timeout=HERDR_TIMEOUT_S, check=False)
        return json.loads(done.stdout) if done.returncode == 0 else None
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None


def agent_panes(listing: object) -> dict[str, int] | None:
    if not isinstance(listing, dict) or "result" not in listing:
        return None
    counts = dict.fromkeys(HARNESSES, 0)
    for pane in listing["result"].get("panes", []):
        if pane.get("agent") in counts:
            counts[pane["agent"]] += 1
    return counts


def panes(now_s: float) -> dict | None:
    """Agent panes by harness on the main herdr server and each enabled machine saved on it, reread at most every
    PANES_FRESH_S; None where herdr is not installed."""
    binary = shutil.which("herdr")
    if binary is None:
        return None
    with _panes_lock:
        if _panes and now_s - _panes["at"] < PANES_FRESH_S:
            return _panes["data"]
        saved = herdr(binary, "machine", "list", "--json")
        machines = [m["label"] for m in saved if m.get("enabled")] if isinstance(saved, list) else []
        targets = [(), *(("--machine", label) for label in machines)]
        with ThreadPoolExecutor(max_workers=len(targets)) as pool:
            counted = list(pool.map(lambda target: agent_panes(herdr(binary, *target, "pane", "list")), targets))
        found = [counts for counts in counted if counts is not None]
        agents = {harness: sum(counts[harness] for counts in found) for harness in HARNESSES}
        data = {"source": "herdr", "reporting": len(found), "of": len(targets), **agents}
        _panes.update(at=now_s, data=data)
        return data


def hive_agents(store: object, readings: dict) -> dict:
    agents, reporting = dict.fromkeys(HARNESSES, 0), 0
    for hive_id, reading in readings.items():
        counts = processes(store, hive_id, reading)
        if counts is None:
            continue
        reporting += 1
        for harness, count in counts.items():
            agents[harness] += count
    return {"source": "hives", "reporting": reporting, "of": len(readings), **agents}


def view(store: object, now_ms: int) -> dict:
    registry, headroom = ports.hive().registry, ports.hive().headroom
    running = sum(store.config(slug).state == "running" for slug in store.slugs())
    live = [hive for hive in registry.hives(store.redis) if registry.live(hive, now_ms)]
    readings = {hive["id"]: headroom.read(store.redis, hive["id"], now_ms) for hive in live}
    measured = [reading for reading in readings.values() if reading is not None]
    agents = panes(now_ms / 1000) or hive_agents(store, readings)
    used, cores, used_mb, total_mb = (
        (sum(column) for column in zip(*map(capacity, measured))) if measured else (0, 0, 0, 0)
    )
    return {
        "agents": {"total": sum(agents[harness] for harness in HARNESSES), **agents},
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
