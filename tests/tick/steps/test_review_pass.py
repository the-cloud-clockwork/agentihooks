"""The review pass tick step registers with its group, lane, feature and ports."""

from importlib import import_module

import pytest

from scripts.tick import registry


@pytest.mark.feature("swarm-review-trigger")
def test_review_pass() -> None:
    """The review pass step registers in group wrapper phases on the tick lane with its feature and ports."""
    import_module("scripts.tick.steps.review")
    found = {step.name: step for step in registry.steps()}["review pass"]
    assert (found.group, found.lane, found.feature, found.effects) == (
        "wrapper phases",
        "tick",
        "swarm-review-trigger",
        frozenset({"ledger"}),
    )
