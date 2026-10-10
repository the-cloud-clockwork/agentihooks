import json

import pytest

from scripts.gates import log as gate_log
from scripts.swarm import autoscale, capacity, lane_split
from scripts.swarm.autoscale import calculate
from scripts.swarm.store import MANUAL_SCALING, RedisStore, SwarmConfig

pytestmark = pytest.mark.xdist_group("fakeredis")

SLUG = "sw"
NOW = 1_800_000_000_000
READY = lane_split.Lanes(ready={"eng": 3, "ci": 2}, live={"eng": 1, "ci": 1}, room=4)


@pytest.fixture
def store():
    import fakeredis

    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(SwarmConfig(SLUG, "/repo", max_eng=4, max_ci=1, scaling=MANUAL_SCALING))
    return saved


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(gate_log, "swarm_home", lambda: tmp_path)
    return tmp_path


def report(store, named, at):
    store.redis.set(store.key(SLUG, "bottleneck"), json.dumps({"at": at, "bottleneck": named}))


def ticks(store, named, count, lanes=READY, start=0):
    actions = []
    for n in range(count):
        report(store, named, NOW + (start + n) * 60_000)
        actions += lane_split.lane_pass(SLUG, store.config(SLUG), store, lanes, NOW + (start + n) * 60_000)
    return actions


def caps(store):
    config = store.config(SLUG)
    return config.max_eng, config.max_ci


def test_a_ci_bottleneck_held_three_ticks_moves_one_seat_from_engineers_to_ci(store, home):
    actions = ticks(store, "ci", 3)
    assert caps(store) == (3, 2)
    assert actions == [lane_split.MOVED.format(giver="eng", taker="ci", named="ci", ticks=3, eng=3, ci=2, total=5)]
    (row,) = gate_log.recent(SLUG, None, home)
    assert (row["gate"], row["kind"], row["agent"], row["tool"], row["at"]) == (
        "dispatcher",
        "apply",
        "dispatcher",
        "lane-split",
        NOW + 120_000,
    )
    assert row["reason"] == actions[0] + " Host room 4."


def test_one_or_two_ticks_move_nothing(store, home):
    assert ticks(store, "ci", 1) == []
    assert caps(store) == (4, 1)
    assert ticks(store, "ci", 1, start=1) == []
    assert caps(store) == (4, 1)
    assert gate_log.recent(SLUG, None, home) == []


def test_a_repeated_report_is_not_a_new_tick(store, home):
    report(store, "ci", NOW)
    for _ in range(5):
        assert lane_split.lane_pass(SLUG, store.config(SLUG), store, READY, NOW) == []
    assert caps(store) == (4, 1)


def test_another_bottleneck_restarts_the_count(store, home):
    ticks(store, "ci", 2)
    ticks(store, "review", 1, start=2)
    assert ticks(store, "ci", 2, start=3) == []
    assert caps(store) == (4, 1)
    assert ticks(store, "ci", 1, start=5) != []
    assert caps(store) == (3, 2)


def test_engineering_moves_the_seat_back_and_the_sum_stays_constant(store, home):
    ticks(store, "ci", 3)
    ticks(store, "engineering", 3, start=3)
    assert caps(store) == (4, 1)
    sums = []
    for n in range(4):
        ticks(store, "ci", 3, start=6 + 3 * n)
        sums.append(sum(caps(store)))
    assert sums == [5, 5, 5, 5]
    assert caps(store) == (1, 4)


def test_the_count_restarts_after_a_move(store, home):
    ticks(store, "ci", 3)
    assert ticks(store, "ci", 2, start=3) == []
    assert caps(store) == (3, 2)
    ticks(store, "ci", 1, start=5)
    assert caps(store) == (2, 3)


def test_a_lane_with_ready_work_keeps_its_last_seat(store, home):
    store.update(SLUG, max_eng=1, max_ci=1)
    actions = ticks(store, "ci", 3)
    assert caps(store) == (1, 1)
    assert actions == [lane_split.HELD.format(named="ci", reason="the eng lane keeps its last seat for ready work")]
    assert gate_log.recent(SLUG, None, home) == []


def test_a_lane_without_ready_work_gives_its_last_seat(store, home):
    store.update(SLUG, max_eng=1, max_ci=1)
    idle = lane_split.Lanes(ready={"eng": 0, "ci": 2}, live={"eng": 0, "ci": 1}, room=4)
    ticks(store, "ci", 3, idle)
    assert caps(store) == (0, 2)
    store.update(SLUG, max_eng=0, max_ci=2)
    assert ticks(store, "ci", 3, idle, start=3) == [
        lane_split.HELD.format(named="ci", reason="the eng lane has no seat to give")
    ]


def test_the_receiving_lane_never_passes_host_room(store, home):
    tight = lane_split.Lanes(ready={"eng": 3, "ci": 2}, live={"eng": 1, "ci": 1}, room=0)
    actions = ticks(store, "ci", 3, tight)
    assert caps(store) == (4, 1)
    assert actions == [lane_split.HELD.format(named="ci", reason="the ci lane would pass host room 0")]
    unknown = lane_split.Lanes(ready={"eng": 3, "ci": 2}, live={"eng": 1, "ci": 1}, room=None)
    ticks(store, "ci", 3, unknown, start=3)
    assert caps(store) == (3, 2)
    (row,) = gate_log.recent(SLUG, None, home)
    assert row["reason"].endswith(" Host room unknown.")


def test_seats_already_live_do_not_need_host_room(store, home):
    busy = lane_split.Lanes(ready={"eng": 3, "ci": 2}, live={"eng": 1, "ci": 3}, room=0)
    ticks(store, "ci", 3, busy)
    assert caps(store) == (3, 2)


@pytest.mark.parametrize("autonomy", ["manual", "assist"])
def test_below_delegate_nothing_moves(store, home, autonomy):
    store.update(SLUG, autonomy=autonomy)
    assert ticks(store, "ci", 4) == []
    assert caps(store) == (4, 1)
    assert store.redis.get(store.key(SLUG, lane_split.KEY)) is None


def test_full_autonomy_moves_like_delegate(store, home):
    store.update(SLUG, autonomy="full")
    ticks(store, "ci", 3)
    assert caps(store) == (3, 2)


def test_no_report_moves_nothing(store, home):
    assert lane_split.lane_pass(SLUG, store.config(SLUG), store, READY, NOW) == []


def test_auto_scaling_moves_the_stored_shift_against_the_last_ceilings(store, home):
    store.update(SLUG, scaling="auto")
    ceilings = {"plan": 1, "ci": 1, "eng": 4}
    store.redis.set(store.key(SLUG, "quota-capacity"), json.dumps({"autoscale": {"ceilings": ceilings}}))
    actions = ticks(store, "ci", 3)
    assert store.config(SLUG).lane_shift == 1
    assert caps(store) == (4, 1)
    assert actions == [lane_split.MOVED.format(giver="eng", taker="ci", named="ci", ticks=3, eng=3, ci=2, total=5)]
    moved = {"plan": 1, "ci": 2, "eng": 3}
    store.redis.set(store.key(SLUG, "quota-capacity"), json.dumps({"autoscale": {"ceilings": moved}}))
    ticks(store, "engineering", 3, start=3)
    assert store.config(SLUG).lane_shift == 0


def test_auto_scaling_without_ceilings_reads_the_stored_caps(store, home):
    store.update(SLUG, scaling="auto")
    ticks(store, "engineering", 3, lane_split.Lanes(ready={"eng": 3, "ci": 0}, live={"eng": 1, "ci": 0}, room=4))
    assert store.config(SLUG).lane_shift == -1


def test_shift_moves_ceilings_and_keeps_the_sum():
    base = calculate({"eng": 1, "ci": 1}, {"claude": 4}, 10, {"eng": 3, "ci": 3})
    shifted = calculate({"eng": 1, "ci": 1}, {"claude": 4}, 10, {"eng": 3, "ci": 3}, shift=1)
    assert base["ceilings"] == {"plan": 0, "ci": 1, "eng": 1}
    assert shifted["ceilings"] == base["ceilings"]
    previous = {"ceilings": {"plan": 0, "ci": 1, "eng": 5}, "pending_raise": {"target": None, "ticks": 0}}
    wide = calculate({"eng": 1, "ci": 1}, {"claude": 4}, 10, {"eng": 3, "ci": 3}, previous, shift=2)
    plain = calculate({"eng": 1, "ci": 1}, {"claude": 4}, 10, {"eng": 3, "ci": 3}, previous)
    assert plain["ceilings"] == {"plan": 0, "ci": 4, "eng": 2}
    assert wide["ceilings"] == {"plan": 0, "ci": 5, "eng": 1}
    assert sum(wide["ceilings"].values()) == sum(plain["ceilings"].values())
    assert wide["reason"].endswith("; lane shift 1 from eng to ci.")


def test_a_shift_back_moves_ci_ceilings_to_engineers():
    previous = {"ceilings": {"plan": 0, "ci": 4, "eng": 2}, "pending_raise": {"target": None, "ticks": 0}}
    decision = calculate({"eng": 1, "ci": 1}, {"claude": 4}, 10, {"eng": 3, "ci": 3}, previous, shift=-2)
    assert decision["ceilings"] == {"plan": 0, "ci": 2, "eng": 4}
    assert decision["reason"].endswith("; lane shift 2 from ci to eng.")


def test_a_shift_keeps_live_agents_and_one_seat_for_ready_work():
    previous = {"ceilings": {"plan": 0, "ci": 1, "eng": 5}, "pending_raise": {"target": None, "ticks": 0}}
    live = calculate({"eng": 3, "ci": 0}, {"claude": 4}, 10, {"eng": 0, "ci": 3}, previous, shift=9)
    assert live["ceilings"]["eng"] == 3
    ready = calculate({"eng": 0, "ci": 0}, {"claude": 4}, 10, {"eng": 1, "ci": 3}, previous, shift=9)
    assert ready["ceilings"]["eng"] == 1
    idle = calculate({"eng": 0, "ci": 0}, {"claude": 4}, 10, {"eng": 0, "ci": 3}, previous, shift=9)
    assert idle["ceilings"]["eng"] == 0


def test_no_shift_leaves_the_decision_unchanged():
    previous = {"ceilings": {"plan": 0, "ci": 1, "eng": 5}, "pending_raise": {"target": None, "ticks": 0}}
    args = ({"eng": 1, "ci": 1}, {"claude": 4}, 10, {"eng": 3, "ci": 3}, previous)
    assert calculate(*args, shift=0) == calculate(*args)


def test_a_config_stored_before_the_lane_shift_reads_zero(store):
    store.redis.hdel(store.key(SLUG, "config"), "lane_shift")
    assert store.config(SLUG).lane_shift == 0
    store.update(SLUG, lane_shift=-2)
    assert store.config(SLUG).lane_shift == -2


def test_autoscaled_passes_the_stored_lane_shift(monkeypatch):
    seen = []
    decision = {"ceilings": {"plan": 0, "ci": 1, "eng": 1}, "pending_raise": {"target": None, "ticks": 0}, "reason": ""}
    monkeypatch.setattr(autoscale, "calculate", lambda *args: seen.append(args[-1]) or decision)
    config = SwarmConfig(SLUG, "/repo", max_eng=1, max_ci=0, lane_shift=-2)
    scaled, _ = capacity.autoscaled(config, capacity.ScaleInputs([], [], None, lambda: None, {}))
    assert seen == [-2]
    assert (scaled.max_eng, scaled.max_ci) == (1, 1)
