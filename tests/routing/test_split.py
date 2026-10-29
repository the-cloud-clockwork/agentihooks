import random

import pytest

from scripts.routing.slots import API, Slot
from scripts.routing.split import choose_side

API_SLOTS = [Slot("claude", "api", 10, 0, kind=API)]
POOL_SLOTS = [Slot("claude", "a", 6, 0)]


def _side(weight, api_live, pool_live, api_room=True, api=API_SLOTS, pool=POOL_SLOTS):
    return choose_side(api, pool, weight, api_live, pool_live, api_room)


@pytest.mark.parametrize(
    ("weight", "api_live", "pool_live", "side"),
    [
        (0, 0, 0, "pool"),
        (0, 0, 9, "pool"),
        (25, 0, 0, "api"),
        (25, 1, 5, "api"),
        (25, 2, 3, "pool"),
        (25, 5, 0, "pool"),
        (80, 0, 0, "api"),
        (80, 23, 5, "api"),
        (80, 4, 0, "pool"),
        (80, 13, 2, "pool"),
        (100, 0, 0, "api"),
        (100, 50, 0, "api"),
        (100, 9, 9, "api"),
    ],
)
def test_the_weight_table_picks_the_side_under_its_target_share(weight, api_live, pool_live, side):
    assert _side(weight, api_live, pool_live) == side


def test_the_boundaries_hold_exactly():
    assert _side(1, 0, 0) == "api"
    assert _side(50, 1, 1) == "api"
    assert _side(50, 2, 2) == "api"
    assert _side(50, 3, 2) == "pool"


def test_an_exact_tie_goes_to_the_pool():
    assert _side(80, 4, 0) == "pool"
    assert _side(50, 1, 0) == "pool"


def test_no_api_slot_gives_the_pool_even_with_no_pool():
    assert _side(100, 0, 0, api=[]) == "pool"
    assert _side(100, 0, 0, api=[], pool=[]) == "pool"


def test_an_exhausted_pool_gives_the_api_only_while_it_has_room():
    assert _side(0, 7, 3, pool=[]) == "api"
    assert _side(0, 7, 3, api_room=False, pool=[]) is None


def test_a_reached_api_cap_gives_the_pool():
    assert _side(100, 4, 0, api_room=False) == "pool"
    assert _side(25, 0, 9, api_room=False) == "pool"


@pytest.mark.parametrize("weight", [0, 25, 80, 100])
def test_simulated_launches_and_exits_converge_on_the_target(weight):
    rng = random.Random(weight)
    live = {"api": 0, "pool": 0}
    for _ in range(100):
        if rng.random() < 0.3 and live["api"] + live["pool"]:
            gone = rng.choice([side for side, count in live.items() for _ in range(count)])
            live[gone] -= 1
        live[_side(weight, live["api"], live["pool"])] += 1
    total = live["api"] + live["pool"]
    assert total > 30
    assert abs(live["api"] - total * weight / 100) <= 1
