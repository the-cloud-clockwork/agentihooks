"""Every swarm runs on the events engine: no tick step launches an agent."""

import pytest

from scripts.tick import registry

pytestmark = pytest.mark.unit

LAUNCH_STEPS = frozenset({"spawn", "spawn due"})


@pytest.mark.feature("swarm-events-only-launch")
def test_no_registered_tick_step_launches_an_agent() -> None:
    names = {step.name for step in registry.steps()}
    assert sorted(names & LAUNCH_STEPS) == []
