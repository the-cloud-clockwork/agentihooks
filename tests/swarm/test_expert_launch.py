"""The expert launch pin: its constants, the pin that puts any pick on it and the check that a launch is on it."""

import pytest

from scripts.swarm import expert_launch
from scripts.swarm.model_pick import ModelPick
from scripts.swarm.tick import SpawnError

pytestmark = pytest.mark.unit


def test_the_pin_is_claude_code_on_fable_at_xhigh_or_max() -> None:
    assert (expert_launch.LANE, expert_launch.HARNESS, expert_launch.MODEL) == ("expert", "claude", "fable")
    assert expert_launch.EFFORTS == ("xhigh", "max")
    assert expert_launch.DEFAULT_EFFORT == "xhigh"
    assert expert_launch.EFFORT_KEY == "expert_effort"


@pytest.mark.parametrize("lane", ["eng", "ci", "plan", "master", "dispatch", ""])
def test_another_lanes_pick_is_left_as_it_is(lane: str) -> None:
    picked = ModelPick("opus", "medium", source="band", confidence=0.9)
    assert expert_launch.pin(lane, picked, "codex", "max") is picked


@pytest.mark.feature("expert-kind-and-launch-pin")
@pytest.mark.parametrize(
    "effort,held",
    [("max", "max"), ("xhigh", "xhigh"), ("", "xhigh"), (None, "xhigh"), ("high", "xhigh"), ("MAX", "xhigh")],
)
def test_an_expert_pick_is_pinned_to_fable_at_the_effort_asked_for_or_xhigh(effort: str | None, held: str) -> None:
    picked = ModelPick("opus", "medium", source="handoff", confidence=0.4)
    assert expert_launch.pin("expert", picked, "claude", effort) == ModelPick("fable", held, source="expert")


@pytest.mark.parametrize("harness", ["codex", "", "copilot"])
def test_an_expert_pick_on_another_harness_is_refused(harness: str) -> None:
    picked = ModelPick("gpt-6.1-sol", "high")
    with pytest.raises(SpawnError) as refused:
        expert_launch.pin("expert", picked, harness, "max")
    assert str(refused.value) == f"an expert launches on claude, never on {harness or 'no harness'}"
    assert refused.value.status == "unsupported"


@pytest.mark.feature("expert-kind-and-launch-pin")
@pytest.mark.parametrize(
    "harness,model,effort,on",
    [
        ("claude", "fable", "xhigh", True),
        ("claude", "fable", "max", True),
        ("claude", "claude-fable-5-2", "max", True),
        ("claude", "claude-fable-5-2[1m]", "xhigh", True),
        ("claude", "fable-old", "xhigh", False),
        ("claude", "claude-fable-lite", "xhigh", False),
        ("claude", "opus", "xhigh", False),
        ("claude", "", "xhigh", False),
        ("codex", "fable", "xhigh", False),
        ("claude", "fable", "high", False),
        ("claude", "fable", "", False),
        ("codex", "opus", "max", False),
    ],
)
def test_a_recorded_launch_is_on_the_pin_only_with_claude_a_fable_model_and_a_pin_effort(
    harness: str, model: str, effort: str, on: bool
) -> None:
    assert expert_launch.on_pin(harness, model, effort) is on
