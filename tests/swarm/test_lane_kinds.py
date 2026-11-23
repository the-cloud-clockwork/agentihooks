"""The agent kinds, lane profiles, launch ranges and moving seats a new lane must leave as they are."""

from collections.abc import Collection

import pytest

from scripts.events import scope as event_scope
from scripts.hive import lanes as hive_lanes
from scripts.inbox import exits
from scripts.swarm import capacity, effort_range, naming, stall_watch, templates
from scripts.swarm import tick as swarm_tick
from scripts.swarm.store import SwarmConfig
from scripts.swarm.templates import DEFAULT_PROFILES
from scripts.swarm_ledger import ledger_tasks
from scripts.swarm_v2.runtime.base import HIVE_LANES

pytestmark = pytest.mark.unit

TABLES: dict[str, Collection[str]] = {
    "default profile": templates.DEFAULT_PROFILES,
    "configurable lane": templates.LANES,
    "lane cap": templates.DEFAULT_CAPS,
    "capacity lane": capacity.LANES,
    "tick task lane": swarm_tick.LANES,
    "hive task lane": hive_lanes.LANES,
    "event scope lane": event_scope.LANES,
    "ledger task lane": ledger_tasks.LANES,
    "stall watched lane": stall_watch.LANES,
    "hive placed lane": HIVE_LANES,
}
TASK_LANE = {*TABLES, "moving numbered seat", "capped seat"}
PLACED = {
    "master": {"default profile", "configurable lane", "lane cap"},
    "eng": TASK_LANE,
    "ci": TASK_LANE,
    "plan": TASK_LANE - {"stall watched lane"},
    "review": {
        "default profile",
        "configurable lane",
        "lane cap",
        "capacity lane",
        "tick task lane",
        "ledger task lane",
        "stall watched lane",
    },
    "dispatch": {"default profile", "hive placed lane"},
    "expert": {"default profile", "hive placed lane", "moving numbered seat"},
}

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


def test_every_kind_sits_in_each_lane_table_on_purpose() -> None:
    found: dict[str, set[str]] = {}
    for lane in naming.TYPES:
        seat = f"{lane}-2@sw"
        found[lane] = {name for name, table in TABLES.items() if lane in table}
        found[lane] |= {"moving numbered seat"} if exits.MOVING_SEAT.fullmatch(seat) else set()
        found[lane] |= {"capped seat"} if exits.LANE_SEAT.fullmatch(seat) else set()
    assert found == PLACED
