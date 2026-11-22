"""The launch an expert seat is held to: Claude Code on the Fable model at effort xhigh or max.

An expert lineage runs one harness and one model whatever the swarm, a lane setting, a saved handoff launch or a
recorded session says. Its effort is the lineage's own: xhigh unless the operator chose max when it started.
"""

from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.swarm.model_pick import ModelPick

LANE = "expert"
HARNESS = "claude"
MODEL = "fable"
EFFORTS = ("xhigh", "max")
DEFAULT_EFFORT = EFFORTS[0]
EFFORT_KEY = "expert_effort"
SOURCE = "expert"


def hold(lane: str, picked: "ModelPick", harness: str, wanted: str | None) -> "ModelPick":
    """The pick an expert launches with whatever was picked, saved or recorded; another lane's pick unchanged."""
    if lane != LANE:
        return picked
    if harness != HARNESS:
        from scripts.swarm.tick import SpawnError

        raise SpawnError(f"an expert launches on {HARNESS}, never on {harness or 'no harness'}", "unsupported")
    effort = wanted if wanted in EFFORTS else DEFAULT_EFFORT
    return replace(picked, model=MODEL, effort=effort, source=SOURCE, confidence=None)
