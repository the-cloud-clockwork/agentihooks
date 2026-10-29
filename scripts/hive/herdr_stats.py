"""Agent panes the master herdr client counts on itself and on every machine saved on it, published to the swarm Redis
so the HOME monitor reads the same numbers wherever its server runs."""

import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from scripts.hive import registry

if TYPE_CHECKING:
    from redis import Redis

EVERY_S = 10
STALE_MS = 30_000
TIMEOUT_S = 8
HARNESSES = ("claude", "codex")
MASTER = "master"


def herdr(binary: str, *args: str) -> object | None:
    try:
        done = subprocess.run([binary, *args], capture_output=True, text=True, timeout=TIMEOUT_S, check=False)
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


def measure(binary: str) -> dict:
    """Agent panes by harness on this herdr client and each enabled machine saved on it, read in parallel; a machine
    that does not answer is named as silent."""
    saved = herdr(binary, "machine", "list", "--json")
    labels = [machine["label"] for machine in saved if machine.get("enabled")] if isinstance(saved, list) else []
    targets = [(MASTER, ()), *((label, ("--machine", label)) for label in labels)]
    with ThreadPoolExecutor() as pool:
        counted = list(pool.map(lambda target: agent_panes(herdr(binary, *target[1], "pane", "list")), targets))
    machines = {name: counts for (name, _), counts in zip(targets, counted) if counts is not None}
    return {"machines": machines, "silent": [name for (name, _), counts in zip(targets, counted) if counts is None]}


def key(hive: str) -> str:
    """Kept under the publishing hive's own record, which every reader of hive records may read."""
    return f"{registry.key(hive)}:herdr"


def publish(redis: "Redis", hive: str, now_ms: int, binary: str | None = None) -> None:
    binary = binary or shutil.which("herdr")
    if binary is not None:
        redis.set(key(hive), json.dumps({**measure(binary), "measured_at": now_ms}), px=STALE_MS)


def read(redis: "Redis", hive: str, now_ms: int) -> dict | None:
    raw = redis.get(key(hive))
    if raw is None:
        return None
    measured = json.loads(raw)
    return measured if now_ms - measured["measured_at"] <= STALE_MS else None
