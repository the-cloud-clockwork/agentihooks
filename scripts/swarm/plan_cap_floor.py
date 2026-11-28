"""Planner cap floor: one planner seat while plan work is ready, and the default planner cap over a stored zero."""

from dataclasses import replace
from typing import Any

from scripts.swarm import autoscale, templates
from scripts.swarm.store import RedisStore, SwarmConfig


def ceilings(caps: dict[str, int], demand: dict[str, int]) -> dict[str, int]:
    """One plan seat while plan work is ready, taken from the larger of the eng and ci ceilings that hold no urgent
    ready work, and from neither when both hold urgent work."""
    if caps["plan"] or not demand.get("plan"):
        return caps
    urgent = autoscale.urgent_counts(demand)
    donors = [lane for lane in ("eng", "ci") if not urgent.get(lane)]
    if not donors:
        return {**caps, "plan": 1}
    donor = max(donors, key=lambda lane: caps[lane])
    return {**caps, donor: max(0, caps[donor] - 1), "plan": 1}


def manual(config: SwarmConfig, demand: dict[str, int]) -> SwarmConfig:
    """A stored zero plan cap counts as one while plan work is ready."""
    if config.max_plan or not demand.get("plan"):
        return config
    return replace(config, max_plan=1)


def restored(config: SwarmConfig, changes: dict[str, Any] | None = None) -> dict[str, int]:
    """The default plan cap for a stored zero, on a start or on a set that raises the eng or ci cap and names no
    plan cap."""
    named = changes or {}
    raised = changes is None or any(named.get(lane, 0) > getattr(config, lane) for lane in ("max_eng", "max_ci"))
    if config.max_plan or "max_plan" in named or not raised:
        return {}
    return {"max_plan": templates.DEFAULT_CAPS["plan"]}


def restore(store: RedisStore, slug: str) -> None:
    """Write the default plan cap over a stored zero as the swarm starts."""
    store.update(slug, **restored(store.config(slug)))
