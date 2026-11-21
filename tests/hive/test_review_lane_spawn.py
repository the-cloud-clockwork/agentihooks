"""The review lane spawns wherever the engineering lane does: workstation, hive routing, spokes, grant and seats."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import fakeredis
import pytest

from scripts.events import scope, subjects
from scripts.hive import lanes, launch, nats_grant
from scripts.swarm import model_pick, naming, profile_choice, readiness, seat_spawn
from scripts.swarm import runtime as swarm_runtime
from scripts.swarm.controller import _hive_lane
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm.templates import DEFAULT_PROFILES
from scripts.swarm_v2.runtime.base import HIVE_LANES, Placement
from tests.hive.test_lanes import configs
from tests.swarm.profile_fixture import validated
from tests.swarm.test_tick import FakeRuntime

pytestmark = pytest.mark.unit

SPAWN_LANE_SETS = [
    pytest.param(swarm_runtime.PICKED_LANES, id="runtime picked lanes"),
    pytest.param(HIVE_LANES, id="version two hive lanes"),
    pytest.param(Placement("kubernetes").lanes, id="kubernetes placement lanes"),
    pytest.param(lanes.LANES, id="hive lane spokes"),
    pytest.param(scope.LANES, id="ready queue lanes"),
]
NOW = 1_800_000_000_000


def _taken(swarm: str, lane: str) -> set[str]:
    stream = subjects.stream_name(swarm, f"QUEUE_{lane}")
    return {f"$JS.API.CONSUMER.MSG.NEXT.{stream}.*", f"$JS.ACK.{stream}.>"}


@pytest.mark.parametrize("found", SPAWN_LANE_SETS)
@pytest.mark.feature("review-lane-spawn")
def test_every_spawn_lane_set_carries_ci_beside_eng(found: frozenset[str] | tuple[str, ...]) -> None:
    assert {"eng", "ci"} <= set(found)


@pytest.mark.parametrize("found", SPAWN_LANE_SETS)
@pytest.mark.feature("review-lane-spawn")
def test_every_spawn_lane_set_carries_review_beside_eng(found: frozenset[str] | tuple[str, ...]) -> None:
    assert {"eng", "review"} <= set(found)


@pytest.mark.feature("review-lane-spawn")
def test_the_hive_member_grant_takes_from_every_ready_queue_lane() -> None:
    granted = set(nats_grant.swarm_publish("sw"))
    for lane in scope.LANES:
        assert _taken("sw", lane) <= granted


@pytest.mark.feature("review-lane-spawn")
def test_the_hive_member_grant_names_the_review_queue() -> None:
    assert _taken("sw", "review") <= set(nats_grant.swarm_publish("sw"))


@pytest.mark.feature("review-lane-spawn")
def test_an_engineering_ready_queue_belongs_to_its_swarm() -> None:
    assert scope.queue_lane("sw", readiness.queue_name("sw", "eng")) == "eng"


@pytest.mark.feature("review-lane-spawn")
def test_the_review_ready_queue_belongs_to_its_swarm() -> None:
    assert scope.queue_lane("sw", readiness.queue_name("sw", "review")) == "review"


@pytest.mark.feature("review-lane-spawn")
def test_each_hive_lane_profile_takes_its_lane_as_hive_role() -> None:
    for lane in lanes.LANES:
        assert launch.ROLE_PROFILES[DEFAULT_PROFILES[lane]] == lane


@pytest.mark.feature("review-lane-spawn")
def test_the_reviewer_profile_takes_the_review_role_on_a_hive() -> None:
    assert launch.ROLE_PROFILES["reviewer"] == "review"


@pytest.mark.parametrize(("lane", "profile"), [("eng", "engineer"), ("review", "reviewer")])
@pytest.mark.feature("review-lane-spawn")
def test_the_hive_controller_places_the_lane_on_a_hive(lane: str, profile: str) -> None:
    placement = Placement(launch.HIVE, lanes=HIVE_LANES, local_profiles=frozenset())
    assert placement.backend_for(lane, {"id": "t1", "profile": profile}) == launch.HIVE
    assert _hive_lane(lane, {}, False)


@pytest.mark.parametrize("lane", ["eng", "review"])
@pytest.mark.feature("review-lane-spawn")
def test_a_kubernetes_proof_keeps_the_lane_off_hives(lane: str) -> None:
    assert not _hive_lane(lane, {}, True)


@pytest.mark.parametrize("lane", ["eng", "review"])
@pytest.mark.feature("review-lane-spawn")
def test_the_kubernetes_backend_places_the_lane(lane: str) -> None:
    assert Placement("kubernetes").backend_for(lane, {"id": "t1"}) == "kubernetes"


@pytest.mark.feature("review-lane-spawn")
def test_a_hive_runs_a_review_spoke_on_the_review_ready_queue(capsys: pytest.CaptureFixture[str]) -> None:
    store, bus, runtime, held = configs({"a": "events"}), Mock(), Mock(), {}
    bus.for_swarm.return_value = bus
    built = lanes.spokes(store, bus, runtime, "hive-a", lanes.lanes("master"), held)
    assert ("a", "review", "hive-a") in [(s.config.swarm.slug, s.config.lane, s.config.hive) for s in built]
    assert readiness.queue_name("a", "review") in capsys.readouterr().err


def _spawned_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lane: str) -> tuple[str, list[tuple]]:
    seen: dict[str, list[str]] = {}
    picks: list[tuple] = []
    real = model_pick.pick

    def pick(*args: Any) -> model_pick.ModelPick:
        picks.append(args)
        return real(*args)

    def run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    monkeypatch.setattr(model_pick, "pick", pick)
    monkeypatch.setattr(profile_choice, "installed", lambda name: True)
    runtime = swarm_runtime.HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "open"))
    config = SimpleNamespace(
        slug="sw", repo=str(tmp_path), code="a1b2c3", compact_limit=0, lanes={}, autonomy="delegate"
    )
    runtime.spawn(config, lane, "agent@a1b2c3-0001", {"id": "t1", "title": "Review the change"})
    argv = seen["argv"]
    return argv[argv.index("--profile") + 1], picks


@pytest.mark.feature("review-lane-spawn")
def test_a_workstation_engineer_spawn_takes_the_model_pick(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile, picks = _spawned_profile(tmp_path, monkeypatch, "eng")
    assert (profile, len(picks)) == ("engineer", 1)


@pytest.mark.feature("review-lane-spawn")
def test_a_workstation_review_spawn_launches_the_reviewer_profile_through_the_model_pick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile, picks = _spawned_profile(tmp_path, monkeypatch, "review")
    assert (profile, len(picks)) == ("reviewer", 1)


@pytest.mark.feature("review-lane-spawn")
def test_seat_spawn_places_a_review_seat() -> None:
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=0, max_ci=0))
    runtime = FakeRuntime()
    record, placed = seat_spawn.place("sw", store.config("sw"), store, runtime, "review", NOW, lambda r: {"id": r.task})
    assert (record.lane, record.task, naming.lane_of(record.name)) == ("review", "reviewer", "review")
    assert placed.pane_id
    assert runtime.spawned == [("review", record.name, "reviewer")]
