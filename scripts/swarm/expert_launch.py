"""The launch an expert seat is held to: Claude Code on the Fable model at effort xhigh or max.

An expert lineage runs one harness and one model whatever the swarm, a lane setting, a saved handoff launch or a
recorded session says. Its effort is the lineage's own: xhigh unless the operator chose max when it started.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.swarm.model_pick import ModelPick

LANE = "expert"
HARNESS = "claude"
MODEL = "fable"
EFFORTS = ("xhigh", "max")
DEFAULT_EFFORT = EFFORTS[0]
EFFORT_KEY = "expert_effort"


def hold(lane: str, picked: "ModelPick", harness: str, wanted: str) -> "ModelPick":
    """The pick an expert launches with whatever was picked, saved or recorded; another lane's pick unchanged."""
    return picked
