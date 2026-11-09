"""Remove retired hive machines while allowing fifteen minutes for a hive restart."""

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.hive import machine, registry
from scripts.hive.auth import HiveError
from scripts.swarm_v2.herdr.capabilities import STATUS_SERVER

if TYPE_CHECKING:
    from redis import Redis

UNREACHABLE_MS = 15 * 60 * 1000
MANAGED = re.compile(r"swarm-hive-[0-9]+")


@dataclass(frozen=True)
class Outage:
    target: str
    since_ms: int


def reachable(label: str, run: Callable[..., subprocess.CompletedProcess[str]]) -> bool:
    try:
        machine.run_checked(run, ["herdr", "--machine", label, *STATUS_SERVER], None, machine.HERDR_TIMEOUT_S)
    except HiveError:
        return False
    return True


def reason(
    label: str,
    target: str,
    registered: bool,
    retired: dict[str, str],
    now_ms: int,
    run: Callable[..., subprocess.CompletedProcess[str]],
    outages: dict[str, Outage],
) -> str:
    if label in retired:
        return retired[label]
    if not registered:
        outages.pop(label, None)
        return "hive removed" if MANAGED.fullmatch(label) else ""
    if reachable(label, run):
        outages.pop(label, None)
        return ""
    observed = outages.get(label)
    if observed is None or observed.target != target:
        outages[label] = Outage(target, now_ms)
        return ""
    if now_ms - observed.since_ms >= UNREACHABLE_MS:
        return f"hive unreachable for {UNREACHABLE_MS // 1000} seconds"
    return ""


def reconcile(
    redis: "Redis",
    known_hosts: Path,
    run: Callable[..., subprocess.CompletedProcess[str]],
    now_ms: int,
    outages: dict[str, Outage],
) -> list[str]:
    hives = {hive["name"] for hive in registry.hives(redis)}
    retired = machine.removed(redis)
    targets = machine.targets(run)
    for label in set(outages) - targets.keys():
        del outages[label]
    lines: list[str] = []
    for label, target in sorted(targets.items()):
        registered = label in hives or machine.named_member(redis, label) is not None
        why = reason(label, target, registered, retired, now_ms, run, outages)
        if why:
            lines.extend(machine.forget(label, known_hosts, run, False, why))
            outages.pop(label, None)
    return lines
