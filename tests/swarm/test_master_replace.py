"""master up --replace, under the swarm's master launch lock, retires a front door master that is still starting before
it starts another, and no new master takes the front door seat from a live occupant."""

from typing import Any

import pytest

from scripts.swarm import master_launch
from scripts.swarm.store import MASTER, AgentRecord, SwarmError
from tests.swarm.test_cli import env  # noqa: F401
from tests.swarm.test_master_launch import ResumingRuntime, up  # noqa: F401

pytestmark = pytest.mark.unit

NOW = 1_791_380_000_000
SEAT = "master@sw"


@pytest.fixture
def swarm(up: tuple[Any, Any, ResumingRuntime]) -> tuple[Any, ResumingRuntime]:  # noqa: F811
    """The created swarm sw with no master, and its runtime."""
    store, _, rt = up
    return store, rt


@pytest.mark.feature("master-launch-lock")
def test_master_up_replace_retires_a_starting_front_door_master_before_it_starts_one(
    swarm: tuple[Any, ResumingRuntime],
) -> None:
    store, rt = swarm
    starting = AgentRecord("master@a1b2c3-0007", MASTER, MASTER, started_at=NOW, state="starting", seat=SEAT)
    store.put_agent("sw", starting)
    store.seats.occupy(SEAT, starting.name, NOW)
    launched = master_launch.up(store, "sw", rt, NOW, master_launch.REPLACE, input, print)
    assert starting.name in rt.homes
    assert [agent.name for agent in store.agents("sw")] == [launched.master]
    assert [name for name, _ in rt.masters] == [launched.master]
    assert store.seats.occupant(SEAT).occupant == launched.master


@pytest.mark.feature("master-launch-lock")
@pytest.mark.parametrize("choice", [master_launch.NEW, master_launch.REPLACE])
def test_master_up_never_takes_the_front_door_seat_from_a_live_occupant(
    swarm: tuple[Any, ResumingRuntime], choice: str
) -> None:
    store, rt = swarm
    store.seats.occupy(SEAT, "master@a1b2c3-0005", NOW)
    rt.live.add("master@a1b2c3-0005")
    with pytest.raises(SwarmError) as refused:
        master_launch.up(store, "sw", rt, NOW, choice, input, print)
    assert str(refused.value) == (
        "the front door seat master@sw is held by the live master@a1b2c3-0005, so no new master takes it"
    )
    assert store.seats.occupant(SEAT).occupant == "master@a1b2c3-0005"
    assert rt.masters == []
    assert store.agents("sw") == []
