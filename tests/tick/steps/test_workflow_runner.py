"""The workflow runner tick step registers with its group, lane, feature and ports."""

from importlib import import_module

import pytest

from scripts.tick import registry


@pytest.mark.feature("swarm-workflow-runner")
def test_workflow_runner() -> None:
    """The workflow runner step registers in group wrapper phases on the slow lane with its feature and ports."""
    import_module("scripts.tick.steps.workflows")
    found = {step.name: step for step in registry.steps()}["workflow runner"]
    assert (found.group, found.lane, found.feature, found.effects) == (
        "wrapper phases",
        "slow",
        "swarm-workflow-runner",
        frozenset({"host", "inbox", "llm"}),
    )
