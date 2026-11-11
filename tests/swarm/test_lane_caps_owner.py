import inspect
import json
from dataclasses import fields

import pytest

from scripts.gates import catalog
from scripts.swarm import bottleneck, capacity, tick
from scripts.swarm.store import AUTONOMY, MANUAL_SCALING, RedisStore, SwarmConfig
from tests.swarm.test_capacity import account

pytestmark = pytest.mark.unit

PREVIOUS = {"autoscale": {"ceilings": {"eng": 6, "ci": 0, "plan": 0}, "pending_raise": {"target": None, "ticks": 0}}}
DEMAND = {"eng": 3, "ci": 5, "plan": 0}
SLUG = "sw"
NOW = 1_800_000_000_000
MINUTE = 60_000


def test_autoscale_splits_the_ceilings_by_lane_demand():
    inputs = capacity.ScaleInputs([account(cap=6)], [], DEMAND, lambda: None, PREVIOUS)
    config = SwarmConfig(SLUG, "/repo", max_eng=6, max_ci=0, max_plan=0, scaling="auto")
    scaled, decision = capacity.autoscaled(config, inputs)
    assert decision["ceilings"] == {"plan": 0, "ci": 3, "eng": 3}
    assert (scaled.max_eng, scaled.max_ci, scaled.max_plan) == (3, 3, 0)
    assert "shift" not in decision
    assert "lane shift" not in decision["reason"]


@pytest.mark.feature("swarm-manual-caps")
@pytest.mark.parametrize(
    "demand",
    [DEMAND, {"eng": 0, "ci": 9, "plan": 0}, {"eng": 9, "ci": 0, "plan": 2}, {"eng": 0, "ci": 0, "plan": 0}],
    ids=str,
)
def test_manual_caps_hold_against_lane_demand(demand):
    inputs = capacity.ScaleInputs([account(cap=6)], [], demand, lambda: None, PREVIOUS)
    config = SwarmConfig(SLUG, "/repo", max_eng=6, max_ci=1, max_plan=0, scaling=MANUAL_SCALING)
    scaled, decision = capacity.autoscaled(config, inputs)
    assert (scaled.max_eng, scaled.max_ci, scaled.max_plan) == (6, 1, 0)
    assert decision is None


def test_the_swarm_config_carries_no_lane_shift_or_lane_split_gate():
    assert "lane_shift" not in {field.name for field in fields(SwarmConfig)}
    assert "lane-split" not in catalog.defaults()


@pytest.mark.parametrize("shift", ["-16", "0", "2", "16"])
def test_a_stored_config_saves_without_the_lane_shift_and_loads_one_left_by_an_older_writer(shift):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(SLUG, "/repo", max_eng=4, max_ci=2))
    key = store.key(SLUG, "config")
    assert "lane_shift" not in store.redis.hgetall(key)
    store.redis.hset(key, mapping={"lane_shift": shift, "gates": json.dumps({"lane-split": "enforce"})})
    config = store.config(SLUG)
    assert (config.max_eng, config.max_ci) == (4, 2)
    assert "lane-split" not in catalog.current(config.gates)
    store.update(SLUG, max_eng=5)
    assert store.config(SLUG).max_eng == 5


def test_the_tick_runs_no_lane_split():
    assert "lane_split" not in inspect.getsource(tick)


@pytest.mark.parametrize("autonomy", AUTONOMY)
def test_a_lane_bottleneck_held_across_ticks_leaves_the_caps(autonomy):
    import fakeredis

    from tests.swarm.test_tick import FakeLedger, FakeRuntime

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    caps = {"max_eng": 4, "max_ci": 2, "scaling": MANUAL_SCALING}
    store.create(SwarmConfig(SLUG, "/repo", autonomy=autonomy, **caps))
    for n in range(4):
        at = NOW + n * MINUTE
        seconds = {"engineering": 0.0, "ci": 60.0, "review": 0.0, "host": 0.0, "quota": 0.0}
        report = {"bottleneck": "ci", "window_hours": 4, "total": 60.0, "seconds": seconds, "at": at}
        store.redis.set(store.key(SLUG, bottleneck.KEY), json.dumps(report))
        tick.tick(SLUG, store, FakeLedger([{"id": "a", "lane": "ci"}]), FakeRuntime(), now_ms=at + 1)
    saved = store.config(SLUG)
    assert (saved.max_eng, saved.max_ci) == (4, 2)
