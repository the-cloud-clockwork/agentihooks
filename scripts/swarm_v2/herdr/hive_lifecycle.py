"""Remove retired hive machines while allowing fifteen minutes for a hive restart."""

import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from scripts.hive import machine, registry
from scripts.hive.auth import HiveError
from scripts.swarm_v2.herdr.capabilities import STATUS_SERVER

if TYPE_CHECKING:
    from redis import Redis

UNREACHABLE_MS = 15 * 60 * 1000
MANAGED = re.compile(r"swarm-hive-[0-9]+")


def reachable(label: str, run: Callable[..., subprocess.CompletedProcess[str]]) -> bool:
    try:
        machine.run_checked(run, ["herdr", "--machine", label, *STATUS_SERVER], None, machine.HERDR_TIMEOUT_S)
    except HiveError:
        return False
    return True


def reason(
    label: str,
    hives: dict[str, dict[str, Any]],
    retired: dict[str, str],
    now_ms: int,
    run: Callable[..., subprocess.CompletedProcess[str]],
) -> str:
    if label in retired:
        return retired[label]
    hive = hives.get(label)
    if hive is None:
        return "hive removed" if MANAGED.fullmatch(label) else ""
    beat = hive["heartbeat_at"]
    if beat > 0 and now_ms - beat >= UNREACHABLE_MS and not reachable(label, run):
        return f"hive unreachable for {UNREACHABLE_MS // 1000} seconds"
    return ""


def reconcile(
    redis: "Redis",
    known_hosts: Path,
    run: Callable[..., subprocess.CompletedProcess[str]],
    now_ms: int,
) -> list[str]:
    hives = {hive["name"]: hive for hive in registry.hives(redis)}
    retired = machine.removed(redis)
    lines: list[str] = []
    for label in sorted(machine.labels(run)):
        why = reason(label, hives, retired, now_ms, run)
        if why:
            lines.extend(machine.forget(label, known_hosts, run, False, why))
    return lines
