"""The agent kinds, lane profiles, launch ranges and moving seats a new lane must leave as they are."""

import pytest

from scripts.inbox import exits
from scripts.swarm import effort_range, naming
from scripts.swarm.store import SwarmConfig
from scripts.swarm.templates import DEFAULT_PROFILES

pytestmark = pytest.mark.unit

KINDS = [
    ("master", "master", "master", "master-2"),
    ("eng", "engineer", "eng-1", "eng-2"),
    ("ci", "ci", "ci-1", "ci-2"),
    ("plan", "planner", "plan-1", "plan-2"),
    ("dispatch", "dispatcher", "dispatch", "dispatch-2"),
]


@pytest.mark.parametrize("lane,kind,first,second", KINDS)
def test_every_kind_keeps_its_name_its_lane_and_its_seat_labels(lane: str, kind: str, first: str, second: str) -> None:
    name = naming.build(kind, "a1b2c3", 7)
    parsed = naming.parse(name)
    assert name == f"{kind}@a1b2c3-0007"
    assert parsed is not None
    assert (parsed.kind, parsed.code, parsed.number, parsed.lane) == (kind, "a1b2c3", 7, lane)
    assert naming.lane_of(name) == lane
    assert (naming.seat_label(lane, 1), naming.seat_label(lane, 2)) == (first, second)


def test_every_lane_keeps_its_default_profile() -> None:
    kept = {"eng": "engineer", "ci": "cicd", "plan": "planner", "master": "master", "dispatch": "dispatcher"}
    assert {lane: DEFAULT_PROFILES[lane] for lane in kept} == kept


@pytest.mark.parametrize("lane", ["eng", "ci", "plan", "dispatch"])
def test_a_worker_or_dispatcher_launch_range_is_the_swarm_range(lane: str) -> None:
    config = SwarmConfig("sw", "/repo", 0, 0, effort_min="low", effort_max="high")
    assert effort_range.bounds(config, lane) == ("low", "high")


@pytest.mark.parametrize(
    "seat,moves",
    [
        ("eng-1@sw", True),
        ("ci-2@sw", True),
        ("plan-13@sw", True),
        ("dispatcher@sw", True),
        ("master@sw", False),
        ("master-2@sw", False),
        ("dispatch-2@sw", False),
        ("eng@sw", False),
    ],
)
def test_lane_and_dispatcher_seat_mail_moves_with_its_occupant_and_master_seat_mail_stays(
    seat: str, moves: bool
) -> None:
    assert bool(exits.MOVING_SEAT.fullmatch(seat)) is moves
