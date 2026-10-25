import inspect

import pytest

from scripts.swarm import capacity, tick
from scripts.swarm.store import SwarmConfig
from tests.swarm.test_capacity import account

pytestmark = pytest.mark.unit

PREVIOUS = {"autoscale": {"ceilings": {"eng": 6, "ci": 0, "plan": 0}, "pending_raise": {"target": None, "ticks": 0}}}


@pytest.mark.parametrize("shift", [0, 2, 16, -16])
def test_a_stored_lane_shift_never_moves_the_autoscale_ceilings(shift):
    inputs = capacity.ScaleInputs([account(cap=6)], [], {"eng": 3, "ci": 5, "plan": 0}, lambda: None, PREVIOUS)
    config = SwarmConfig("sw", "/repo", max_eng=6, max_ci=0, max_plan=0, scaling="auto", lane_shift=shift)
    scaled, decision = capacity.autoscaled(config, inputs)
    assert decision["ceilings"] == {"plan": 0, "ci": 3, "eng": 3}
    assert (scaled.max_eng, scaled.max_ci, scaled.max_plan) == (3, 3, 0)
    assert "shift" not in decision
    assert "lane shift" not in decision["reason"]


def test_the_tick_runs_no_lane_split():
    assert "lane_split" not in inspect.getsource(tick)
