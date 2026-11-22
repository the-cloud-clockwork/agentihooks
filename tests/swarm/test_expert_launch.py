"""The expert launch pin: its constants and the hold that puts any pick on it."""

import pytest

from scripts.swarm import expert_launch
from scripts.swarm.model_pick import ModelPick
from scripts.swarm.tick import SpawnError

pytestmark = pytest.mark.unit

FEATURE = "expert-kind-and-launch-pin"


def test_the_pin_is_claude_code_on_fable_at_xhigh_or_max() -> None:
    assert (expert_launch.LANE, expert_launch.HARNESS, expert_launch.MODEL) == ("expert", "claude", "fable")
    assert expert_launch.EFFORTS == ("xhigh", "max")
    assert expert_launch.DEFAULT_EFFORT == "xhigh"
    assert expert_launch.EFFORT_KEY == "expert_effort"


@pytest.mark.parametrize("lane", ["eng", "ci", "plan", "master", "dispatch", ""])
def test_another_lanes_pick_is_left_as_it_is(lane: str) -> None:
    picked = ModelPick("opus", "medium", source="band", confidence=0.9)
    assert expert_launch.hold(lane, picked, "codex", "max") is picked


@pytest.mark.feature(FEATURE)
@pytest.mark.parametrize(
    "wanted,held",
    [("max", "max"), ("xhigh", "xhigh"), ("", "xhigh"), ("high", "xhigh"), ("low", "xhigh"), ("MAX", "xhigh")],
)
def test_an_expert_pick_is_held_to_fable_at_its_lineage_effort_or_xhigh(wanted: str, held: str) -> None:
    picked = ModelPick("opus", "medium", source="handoff", confidence=0.4)
    assert expert_launch.hold("expert", picked, "claude", wanted) == ModelPick("fable", held, source="expert")


@pytest.mark.parametrize("harness", ["codex", "", "copilot"])
def test_an_expert_pick_on_another_harness_is_refused(harness: str) -> None:
    picked = ModelPick("gpt-6.1-sol", "high")
    with pytest.raises(SpawnError) as refused:
        expert_launch.hold("expert", picked, harness, "max")
    assert str(refused.value) == f"an expert launches on claude, never on {harness or 'no harness'}"
    assert refused.value.status == "unsupported"
