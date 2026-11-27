"""The master loop tick step registers with its group, lane, feature and ports."""

from importlib import import_module

import pytest

from scripts.tick import registry


@pytest.mark.feature("master-loop-seat-inbox")
def test_master_loop() -> None:
    """The master loop step registers in group wrapper events on the tick lane with its feature and ports."""
    import_module("scripts.tick.steps.master_loop")
    found = {step.name: step for step in registry.steps()}["master loop"]
    assert (found.group, found.lane, found.feature, found.effects) == (
        "wrapper events",
        "tick",
        "master-loop-seat-inbox",
        frozenset({"coordination", "inbox"}),
    )
