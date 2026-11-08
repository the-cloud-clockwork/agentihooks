"""Spawn deadline boundary."""

import pytest

from scripts.tick.deadline import Deadline
from scripts.tick.steps.place import _past_deadline

pytestmark = pytest.mark.unit


@pytest.mark.feature("swarm-tick-spawn")
@pytest.mark.parametrize(
    "now, expected",
    [(9.0, []), (10.0, ["spawn stopped at the pass deadline, 2 of 3 ready tasks left for the next pass"])],
)
def test_spawn(now: float, expected: list[str]) -> None:
    assert _past_deadline(Deadline(10.0, lambda: now), 2, 3) == expected
