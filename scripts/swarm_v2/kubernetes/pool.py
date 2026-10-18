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


def reading(api: PodApi, namespace: str, pool: dict) -> list[dict] | None:
    """The ceiling is the policy Pod limit and nodes the distinct nodes running live worker Pods, never node groups."""
    try:
        pods = api.list_pods("")
    except (ApiRefused, OSError):
        return None
    live = [pod for pod in pods if pod.get("status", {}).get("phase") not in FINISHED]
    nodes = {pod["spec"].get("nodeName") for pod in live} - {None, ""}
    return [{"pool": namespace, "configured_maximum": pool["pods"], "nodes": len(nodes)}]


def _count(quantity: str) -> int:
    return int(_amount(quantity))


QUOTA_KEYS = (
    (PODS, "pods", _count),
    (PODS, "count/pods", _count),
    (CPU, "requests.cpu", cpu_millis),
    (CPU, "cpu", cpu_millis),
    (CPU, "limits.cpu", cpu_millis),
    (MEMORY, "requests.memory", memory_mib),
    (MEMORY, "memory", memory_mib),
    (MEMORY, "limits.memory", memory_mib),
)
UNITS = {PODS: "{} Pods", CPU: "{}m CPU", MEMORY: "{} MiB memory"}


def _left(quotas: list[dict]) -> dict[str, int]:
    left: dict[str, int] = {}
    for quota in quotas:
        status = quota.get("status", {})
        hard, used = status.get("hard", {}), status.get("used", {})
        for resource, key, read in QUOTA_KEYS:
            if key in hard:
                free = read(hard[key]) - read(used.get(key, "0"))
                left[resource] = min(left.get(resource, free), free)
    return left


def quota_room(quotas: list[dict], launch: dict) -> Room | None:
    left = _left(quotas)
    if not left:
        return None
    each = {PODS: 1, CPU: launch["cpu_millis"], MEMORY: launch["memory_mib"]}
    named = [resource for resource in UNITS if resource in left]
    fits = {resource: left[resource] // each[resource] for resource in named}
    limit = min(named, key=fits.__getitem__)
    room = max(0, fits[limit])
    parts = [UNITS[resource].format(left[resource]) for resource in named]
    listed = f"{', '.join(parts[:-1])} and {parts[-1]}" if len(parts) > 1 else parts[0]
    return Room(
        room,
        f"worker namespace quota leaves {listed}; {room} more fit at {launch['cpu_millis']}m and "
        f"{launch['memory_mib']} MiB each",
        f"quota {limit}",
    )


class WorkerPool:
    """Lists every Pod and quota in the worker namespace, with no label selector."""

    def __init__(self, api: PodApi, pool: dict | None, launch: dict) -> None:
        self.api, self.pool, self.launch = api, pool, launch

    def __call__(self) -> Room:
        readings = [self._quota(), self._pool()]
        known = [reading for reading in readings if reading.room is not None]
        if known:
            return min(known, key=lambda reading: reading.room)
        return Room(None, f"{'; '.join(reading.reason for reading in readings)}, so spawns pass", UNKNOWN)

    def _quota(self) -> Room:
        try:
            found = quota_room(self.api.list_quotas(), self.launch)
        except (ApiRefused, OSError) as error:
            return Room(None, f"worker namespace quota cannot be read ({type(error).__name__})")
        except ValueError as error:
            return Room(None, f"worker namespace quota is {error}")
        return found or Room(None, "worker namespace quota sets no Pod, CPU or memory limit")

    def _pool(self) -> Room:
        if self.pool is None:
            return Room(None, "worker pool undeclared: the Pod policy names no worker_pool")
        try:
            pods = self.api.list_pods("")
        except (ApiRefused, OSError) as error:
            return Room(None, f"worker pool unknown: the worker Pods cannot be listed ({type(error).__name__})")
        try:
            used = usage(pods)
        except ValueError as error:
            return Room(None, f"worker pool unknown: a worker Pod request is {error}")
        return pool_room(self.pool, self.launch, used)
