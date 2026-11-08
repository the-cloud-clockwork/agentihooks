"""Fill a lane successor's saved launch with the lane model and an effort inside the swarm range before placement."""

from importlib import import_module
from typing import Any

from scripts.swarm.store import SwarmConfig

effort_range: Any = import_module("scripts.swarm.effort_range")


def lane_model(chosen: dict[str, Any]) -> str:
    """The lane's pinned model, or an empty string when the lane leaves it automatic."""
    model = chosen.get("model") or ""
    return "" if model == "auto" else model


def filled(saved: dict[str, Any], config: SwarmConfig, lane: str) -> dict[str, Any]:
    """A saved launch whose empty model takes the lane's pinned model and whose empty effort the lane range effort."""
    harness = saved.get("harness", "")
    if harness not in effort_range.EFFORTS or (saved.get("model") and saved.get("effort")):
        return saved
    settings: Any = config
    chosen: dict[str, Any] = settings.lanes.get(lane, {})
    bounds: tuple[str, str] = effort_range.bounds(config, lane)
    wanted = chosen.get("effort") if effort_range.rank(chosen.get("effort")) is not None else bounds[0]
    return {
        **saved,
        "model": saved.get("model") or lane_model(chosen),
        "effort": saved.get("effort") or effort_range.clamp(harness, wanted, bounds),
    }


def task(primed: dict[str, Any], config: SwarmConfig, lane: str) -> dict[str, Any]:
    """The primed task with its handoff envelope launch filled for the lane."""
    envelope: dict[str, Any] = primed.get("handoff_envelope") or {}
    if not envelope.get("launch"):
        return primed
    return {**primed, "handoff_envelope": {**envelope, "launch": filled(envelope["launch"], config, lane)}}
