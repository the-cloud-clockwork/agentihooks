import inspect
import json

import pytest

from scripts.swarm import bottleneck, capacity, tick
from scripts.swarm.store import AUTONOMY, MANUAL_SCALING, RedisStore, SwarmConfig
from tests.swarm.test_capacity import account

pytestmark = pytest.mark.unit

PREVIOUS = {"autoscale": {"ceilings": {"eng": 6, "ci": 0, "plan": 0}, "pending_raise": {"target": None, "ticks": 0}}}
DEMAND = {"eng": 3, "ci": 5, "plan": 0}
SLUG = "sw"
NOW = 1_800_000_000_000
MINUTE = 60_000


@pytest.mark.parametrize("shift", [0, 2, 16, -16])
def test_a_stored_lane_shift_never_moves_the_autoscale_ceilings(shift):
    inputs = capacity.ScaleInputs([account(cap=6)], [], DEMAND, lambda: None, PREVIOUS)
    config = SwarmConfig(SLUG, "/repo", max_eng=6, max_ci=0, max_plan=0, scaling="auto", lane_shift=shift)
    scaled, decision = capacity.autoscaled(config, inputs)
    assert decision["ceilings"] == {"plan": 0, "ci": 3, "eng": 3}
    assert (scaled.max_eng, scaled.max_ci, scaled.max_plan) == (3, 3, 0)
    assert "shift" not in decision
    assert "lane shift" not in decision["reason"]


@pytest.mark.parametrize("shift", [2, 16, -16])
def test_manual_caps_ignore_a_stored_lane_shift(shift):
    inputs = capacity.ScaleInputs([account(cap=6)], [], DEMAND, lambda: None, PREVIOUS)
    config = SwarmConfig(SLUG, "/repo", max_eng=6, max_ci=1, max_plan=0, scaling=MANUAL_SCALING, lane_shift=shift)
    scaled, decision = capacity.autoscaled(config, inputs)
    assert (scaled.max_eng, scaled.max_ci, scaled.max_plan) == (6, 1, 0)
    assert decision is None


def test_the_tick_runs_no_lane_split():
    assert "lane_split" not in inspect.getsource(tick)


@pytest.mark.parametrize("autonomy", AUTONOMY)
def test_a_lane_bottleneck_held_across_ticks_leaves_the_caps_and_the_stored_shift(autonomy):
    import fakeredis

    from tests.swarm.test_tick import FakeLedger, FakeRuntime

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    caps = {"max_eng": 4, "max_ci": 2, "scaling": MANUAL_SCALING, "lane_shift": 16, "gates": {"lane-split": "enforce"}}
    store.create(SwarmConfig(SLUG, "/repo", autonomy=autonomy, **caps))
    for n in range(4):
        at = NOW + n * MINUTE
        report = {"bottleneck": "ci", "window_hours": 4, "total": 60.0, "seconds": {"ci": 60.0}, "at": at}
        store.redis.set(store.key(SLUG, bottleneck.KEY), json.dumps(report))
        tick.tick(SLUG, store, FakeLedger([{"id": "a", "lane": "ci"}]), FakeRuntime(), now_ms=at + 1)
    saved = store.config(SLUG)
    assert (saved.max_eng, saved.max_ci, saved.lane_shift) == (4, 2, 16)
