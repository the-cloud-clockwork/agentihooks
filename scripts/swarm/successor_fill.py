"""Fill a lane successor's saved launch with the lane model and an effort inside the swarm range before placement."""

from typing import Any

from scripts.swarm import default_models, effort_range
from scripts.swarm.store import SwarmConfig


def lane_model(chosen: dict[str, Any]) -> str:
    """The lane's pinned model, or an empty string when the lane leaves it automatic."""
    model = chosen.get("model") or ""
    return "" if model == "auto" else model


def filled(saved: dict[str, Any], config: SwarmConfig, lane: str) -> dict[str, Any]:
    """A saved launch whose empty model or effort is taken from the lane settings and the swarm effort range."""
    harness = saved.get("harness", "")
    if harness not in effort_range.EFFORTS or (saved.get("model") and saved.get("effort")):
        return saved
    chosen = config.lanes.get(lane, {})
    bounds = effort_range.bounds(config, lane)
    wanted = chosen.get("effort") if effort_range.rank(chosen.get("effort")) is not None else bounds[0]
    return {
        **saved,
        "model": saved.get("model") or lane_model(chosen) or default_models.model(config, harness),
        "effort": saved.get("effort") or effort_range.clamp(harness, wanted, bounds),
    }


def task(primed: dict[str, Any], config: SwarmConfig, lane: str) -> dict[str, Any]:
    """The primed task with its saved launch assignment and handoff launch filled for the lane."""
    if primed.get("launch_assignment"):
        primed = {**primed, "launch_assignment": filled(primed["launch_assignment"], config, lane)}
    envelope = primed.get("handoff_envelope") or {}
    if envelope.get("launch"):
        primed = {**primed, "handoff_envelope": {**envelope, "launch": filled(envelope["launch"], config, lane)}}
    return primed
