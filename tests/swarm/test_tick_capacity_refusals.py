from scripts.swarm import capacity
from tests.swarm.test_tick import store as store


def test_a_placement_refusal_skips_capacity_only_until_it_expires(store):
    from scripts.hive import placement_waits

    placement_waits.observe(store, "sw", "ci", "t1", "no hive for ci has room: h1 holds 4 queued launches", 0)
    placement_waits.observe(store, "sw", "ci", "t2", "no hive for ci has room: h1 holds 4 queued launches", 0)
    ready = {"ci": [{"id": "t1"}, {"id": "t2"}, {"id": "t3"}]}
    fresh, skipped = capacity.placement_ready(store, "sw", ready, placement_waits.FRESH_MS - 1)
    assert fresh == {"ci": [{"id": "t3"}]}
    assert len(skipped) == 2
    expired, skipped = capacity.placement_ready(store, "sw", ready, placement_waits.FRESH_MS)
    assert (expired, skipped) == (ready, [])
