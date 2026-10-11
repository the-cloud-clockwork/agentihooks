"""Reads only the worker namespace's Pod list, never a node."""

import math
import re
from dataclasses import dataclass
from decimal import Decimal

from scripts.swarm.host_budget import UNKNOWN, Room
from scripts.swarm_v2.kubernetes.client import ApiRefused, PodApi

FINISHED = ("Succeeded", "Failed")
PODS, CPU, MEMORY = "pods", "cpu", "memory"
QUANTITY = re.compile(r"([0-9]+(?:\.[0-9]*)?|\.[0-9]+)(Ki|Mi|Gi|Ti|Pi|Ei|n|u|m|k|M|G|T|P|E|[eE][+-]?[0-9]+|)")
SUFFIXES = {
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "m": Decimal("1e-3"),
    "": Decimal(1),
    "k": Decimal("1e3"),
    "M": Decimal("1e6"),
    "G": Decimal("1e9"),
    "T": Decimal("1e12"),
    "P": Decimal("1e15"),
    "E": Decimal("1e18"),
    **{suffix: Decimal(1024**power) for power, suffix in enumerate(("Ki", "Mi", "Gi", "Ti", "Pi", "Ei"), 1)},
}
MIB = 1024**2


@dataclass(frozen=True)
class Usage:
    pods: int
    cpu_millis: int
    memory_mib: int


def _amount(quantity: str) -> Decimal:
    found = QUANTITY.fullmatch(quantity)
    refused = ValueError(f"not a resource quantity: {quantity!r}")
    if found is None:
        raise refused
    try:
        return Decimal(found[1]) * (SUFFIXES.get(found[2]) or Decimal(10) ** int(found[2][1:]))
    except ArithmeticError:
        raise refused from None


def cpu_millis(quantity: str) -> int:
    return math.ceil(_amount(quantity) * 1000)


def memory_mib(quantity: str) -> int:
    return math.ceil(_amount(quantity) / MIB)


def _requested(containers: list[dict]) -> list[tuple[int, int]]:
    """Kubernetes defaults a missing request to its limit."""
    resources = [container.get("resources", {}) for container in containers]
    asked = [{**row.get("limits", {}), **row.get("requests", {})} for row in resources]
    return [(cpu_millis(row.get(CPU, "0")), memory_mib(row.get(MEMORY, "0"))) for row in asked]


def requests(pod: dict) -> tuple[int, int]:
    containers = _requested(pod["spec"]["containers"])
    init = _requested(pod["spec"].get("initContainers", []))
    return (
        max(sum(row[0] for row in containers), *(row[0] for row in init), 0),
        max(sum(row[1] for row in containers), *(row[1] for row in init), 0),
    )


def usage(pods: list[dict]) -> Usage:
    live = [requests(pod) for pod in pods if pod.get("status", {}).get("phase") not in FINISHED]
    return Usage(len(live), sum(row[0] for row in live), sum(row[1] for row in live))


def pool_room(pool: dict, launch: dict, used: Usage) -> Room:
    fits = {
        PODS: pool["pods"] - used.pods,
        CPU: (pool["cpu_millis"] - used.cpu_millis) // launch["cpu_millis"],
        MEMORY: (pool["memory_mib"] - used.memory_mib) // launch["memory_mib"],
    }
    limit = min(fits, key=fits.__getitem__)
    room = max(0, fits[limit])
    return Room(
        room,
        f"worker pool: {used.pods} of {pool['pods']} Pods, {used.cpu_millis}m of {pool['cpu_millis']}m CPU and "
        f"{used.memory_mib} MiB of {pool['memory_mib']} MiB memory requested; {room} more fit at "
        f"{launch['cpu_millis']}m and {launch['memory_mib']} MiB each",
        limit,
    )


class WorkerPool:
    """Lists every Pod in the worker namespace, with no label selector."""

    def __init__(self, api: PodApi, pool: dict | None, launch: dict) -> None:
        self.api, self.pool, self.launch = api, pool, launch

    def __call__(self) -> Room:
        if self.pool is None:
            return Room(None, "worker pool undeclared: the Pod policy names no worker_pool, so spawns pass", UNKNOWN)
        try:
            pods = self.api.list_pods("")
        except (ApiRefused, OSError) as error:
            return Room(
                None,
                f"worker pool unknown: the worker Pods cannot be listed ({type(error).__name__}), so spawns pass",
                UNKNOWN,
            )
        try:
            used = usage(pods)
        except ValueError as error:
            return Room(None, f"worker pool unknown: a worker Pod request is {error}, so spawns pass", UNKNOWN)
        return pool_room(self.pool, self.launch, used)
