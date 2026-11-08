"""A hive's agent ceiling: the agents its own headroom reading counts plus the workstation host room rule over that
reading's memory, swap and one minute load, bounded from above by a cap the operator set. The rule runs with no
previous room, so between the load watermarks the ceiling holds at the agents already running."""

from typing import Any

from scripts.swarm import host_budget

READ = ("cpus", "load5", "available_mb")
NO_SWAP = (0, 0)


def computed(measured: dict[str, Any] | None) -> int | None:
    """The ceiling the reading alone allows, or None when there is no usable reading."""
    if measured is None or any(field not in measured for field in READ):
        return None
    agents = sum(measured.get("agents", {}).values())
    swap = (measured["swap_used_mb"], measured["swap_total_mb"]) if "swap_total_mb" in measured else NO_SWAP
    available = min(measured["available_mb"], measured.get("pod_available_mb", measured["available_mb"]))
    sample = host_budget.HostSample(
        load1=measured.get("load1", measured["load5"]),
        cpus=measured["cpus"],
        available_mb=available,
        agents=agents,
        swap_used_mb=swap[0],
        swap_total_mb=swap[1],
    )
    return agents + (host_budget.room(sample).room or 0)


def ceiling(cap: int | None, measured: dict[str, Any] | None) -> int:
    """The computed ceiling held under the cap; the cap alone, or none, without a reading."""
    found = computed(measured)
    if found is None:
        return cap or 0
    return found if cap is None else min(cap, found)


def shown(cap: int | None, measured: dict[str, Any] | None) -> int | None:
    """The ceiling, or None when neither a cap nor a usable reading gives one."""
    if cap is None and computed(measured) is None:
        return None
    return ceiling(cap, measured)
