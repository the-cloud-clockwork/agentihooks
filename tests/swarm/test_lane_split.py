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
        actions += lane_split.lane_pass(SLUG, store.config(SLUG), store, lambda: lanes, NOW + (start + n) * 60_000)
    return actions


def caps(store):
    config = store.config(SLUG)
    return config.max_eng, config.max_ci


def unread():
    raise AssertionError("the lanes are read only when a move is due")


def test_ticks_before_a_move_is_due_never_read_the_lanes(store, home):
    for n in range(2):
        report(store, "ci", NOW + n * 60_000)
        assert lane_split.lane_pass(SLUG, store.config(SLUG), store, unread, NOW + n * 60_000) == []


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
    assert row["task"] == ""
    assert actions[0].startswith(
        "Moved one seat from the eng lane to the ci lane after the bottleneck report named ci 3 ticks"
    )
    assert actions[0].endswith("running: engineers 3, CI 2, sum 5.")
    assert json.loads(store.redis.get(store.key(SLUG, "lane-split")))["ticks"] == 0


def test_one_or_two_ticks_move_nothing(store, home):
    assert ticks(store, "ci", 1) == []
    assert caps(store) == (4, 1)
    assert ticks(store, "ci", 1, start=1) == []
    assert caps(store) == (4, 1)
    assert gate_log.recent(SLUG, None, home) == []


def test_a_repeated_report_is_not_a_new_tick(store, home):
    report(store, "ci", NOW)
    for _ in range(5):
        assert lane_split.lane_pass(SLUG, store.config(SLUG), store, unread, NOW) == []
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
    assert actions == [
        lane_split.HELD.format(named="ci", ticks=3, reason="the eng lane keeps its last seat for ready work")
    ]
    assert actions[0] == (
        "Lane split held after the bottleneck report named ci 3 ticks running:"
        " the eng lane keeps its last seat for ready work."
    )
    assert gate_log.recent(SLUG, None, home) == []


def test_a_lane_without_ready_work_gives_its_last_seat(store, home):
    store.update(SLUG, max_eng=1, max_ci=1)
    idle = lane_split.Lanes(ready={"eng": 0, "ci": 2}, live={"eng": 0, "ci": 1}, room=4)
    ticks(store, "ci", 3, idle)
    assert caps(store) == (0, 2)
    store.update(SLUG, max_eng=0, max_ci=2)
    assert ticks(store, "ci", 3, idle, start=3) == [
        lane_split.HELD.format(named="ci", ticks=3, reason="the eng lane has no seat to give")
    ]


def test_the_giving_lane_keeps_a_seat_for_each_live_agent(store, home):
    busy = lane_split.Lanes(ready={"eng": 0, "ci": 2}, live={"eng": 4, "ci": 1}, room=4)
    actions = ticks(store, "ci", 3, busy)
    assert caps(store) == (4, 1)
    assert actions == [
        lane_split.HELD.format(named="ci", ticks=3, reason="the eng lane keeps a seat for each of its 4 live agents")
    ]
    fewer = lane_split.Lanes(ready={"eng": 0, "ci": 2}, live={"eng": 3, "ci": 1}, room=4)
    ticks(store, "ci", 3, fewer, start=3)
    assert caps(store) == (3, 2)


def test_the_receiving_lane_never_passes_host_room(store, home):
    tight = lane_split.Lanes(ready={"eng": 3, "ci": 2}, live={"eng": 1, "ci": 1}, room=0)
    actions = ticks(store, "ci", 3, tight)
    assert caps(store) == (4, 1)
    assert actions == [lane_split.HELD.format(named="ci", ticks=3, reason="the ci lane would pass host room 0")]
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
    assert lane_split.lane_pass(SLUG, store.config(SLUG), store, unread, NOW) == []


def test_a_report_that_disappears_moves_nothing(store, home):
    ticks(store, "ci", 2)
    store.redis.delete(store.key(SLUG, "bottleneck"))
    assert lane_split.lane_pass(SLUG, store.config(SLUG), store, unread, NOW + 600_000) == []
    assert caps(store) == (4, 1)


def test_a_last_seat_with_live_agents_but_no_ready_work_is_held_by_its_live_agent(store, home):
    store.update(SLUG, max_eng=1, max_ci=1)
    working = lane_split.Lanes(ready={"eng": 0, "ci": 2}, live={"eng": 1, "ci": 1}, room=4)
    assert ticks(store, "ci", 3, working) == [
        lane_split.HELD.format(named="ci", ticks=3, reason="the eng lane keeps a seat for each of its 1 live agents")
    ]
    assert caps(store) == (1, 1)


def test_auto_scaling_moves_the_stored_shift_against_the_last_ceilings(store, home):
    store.update(SLUG, scaling="auto")
    ceilings = {"plan": 1, "ci": 1, "eng": 4}
    store.redis.set(store.key(SLUG, "quota-capacity"), json.dumps({"autoscale": {"ceilings": ceilings}}))
    actions = ticks(store, "ci", 3)
    assert store.config(SLUG).lane_shift == 1
    assert caps(store) == (4, 1)
    assert actions == [lane_split.MOVED.format(giver="eng", taker="ci", named="ci", ticks=3, eng=3, ci=2, total=5)]
    moved = {"ceilings": {"plan": 1, "ci": 2, "eng": 3}, "shift": 1}
    store.redis.set(store.key(SLUG, "quota-capacity"), json.dumps({"autoscale": moved}))
    ticks(store, "engineering", 3, start=3)
    assert store.config(SLUG).lane_shift == 0


def test_an_auto_move_leaves_the_autoscale_record_to_the_next_calculate(store, home):
    store.update(SLUG, scaling="auto")
    record = {"other": 7, "autoscale": {"ceilings": {"plan": 1, "ci": 1, "eng": 4}, "reason": "r"}}
    store.redis.set(store.key(SLUG, "quota-capacity"), json.dumps(record))
    ticks(store, "ci", 3)
    assert store.config(SLUG).lane_shift == 1
    assert json.loads(store.redis.get(store.key(SLUG, "quota-capacity"))) == record


def test_auto_scaling_moves_from_the_shift_calculate_achieved(store, home):
    store.update(SLUG, scaling="auto", lane_shift=3)
    clamped = {"ceilings": {"plan": 1, "ci": 2, "eng": 3}, "shift": 1}
    store.redis.set(store.key(SLUG, "quota-capacity"), json.dumps({"autoscale": clamped}))
    ticks(store, "engineering", 3)
    assert store.config(SLUG).lane_shift == 0


def test_auto_scaling_without_ceilings_reads_the_stored_caps(store, home):
    store.update(SLUG, scaling="auto", lane_shift=2)
    ticks(store, "engineering", 3, lane_split.Lanes(ready={"eng": 3, "ci": 0}, live={"eng": 1, "ci": 0}, room=4))
    assert store.config(SLUG).lane_shift == 1


def test_shift_moves_ceilings_and_keeps_the_sum():
    base = calculate({"eng": 1, "ci": 1}, {"claude": 4}, 10, {"eng": 3, "ci": 3})
    shifted = calculate({"eng": 1, "ci": 1}, {"claude": 4}, 10, {"eng": 3, "ci": 3}, shift=1)
    assert base["ceilings"] == {"plan": 0, "ci": 1, "eng": 1}
    assert "lane shift" not in base["reason"] + shifted["reason"]
    assert shifted["ceilings"] == base["ceilings"]
    previous = {"ceilings": {"plan": 0, "ci": 1, "eng": 5}, "pending_raise": {"target": None, "ticks": 0}}
    wide = calculate({"eng": 1, "ci": 1}, {"claude": 4}, 10, {"eng": 3, "ci": 3}, previous, shift=2)
    plain = calculate({"eng": 1, "ci": 1}, {"claude": 4}, 10, {"eng": 3, "ci": 3}, previous)
    assert plain["ceilings"] == {"plan": 0, "ci": 4, "eng": 2}
    assert wide["ceilings"] == {"plan": 0, "ci": 5, "eng": 1}
    assert sum(wide["ceilings"].values()) == sum(plain["ceilings"].values())
    assert wide["reason"].endswith("; lane shift 1 from eng to ci.")
    assert (wide["shift"], "shift" in plain) == (1, False)


def test_a_shift_back_moves_ci_ceilings_to_engineers():
    previous = {"ceilings": {"plan": 0, "ci": 4, "eng": 2}, "pending_raise": {"target": None, "ticks": 0}}
    decision = calculate({"eng": 1, "ci": 1}, {"claude": 4}, 10, {"eng": 3, "ci": 3}, previous, shift=-2)
    assert decision["ceilings"] == {"plan": 0, "ci": 2, "eng": 4}
    assert decision["reason"].endswith("; lane shift 2 from ci to eng.")
    assert decision["shift"] == -2


def test_a_shift_keeps_live_agents_and_one_seat_for_ready_work():
    previous = {"ceilings": {"plan": 0, "ci": 1, "eng": 5}, "pending_raise": {"target": None, "ticks": 0}}
    live = calculate({"eng": 3, "ci": 0}, {"claude": 4}, 10, {"eng": 0, "ci": 3}, previous, shift=9)
    assert live["ceilings"]["eng"] == 3
    ready = calculate({"eng": 0, "ci": 0}, {"claude": 4}, 10, {"eng": 1, "ci": 3}, previous, shift=9)
    assert ready["ceilings"]["eng"] == 1
    idle = calculate({"eng": 0, "ci": 0}, {"claude": 4}, 10, {"eng": 0, "ci": 3}, previous, shift=9)
    assert idle["ceilings"]["eng"] == 0
    assert (live["shift"], ready["shift"], idle["shift"]) == (0, 0, 1)


def test_a_shift_of_one_moves_one_ceiling_toward_ci():
    previous = {"ceilings": {"plan": 0, "ci": 1, "eng": 5}, "pending_raise": {"target": None, "ticks": 0}}
    one = calculate({"eng": 1, "ci": 1}, {"claude": 4}, 10, {"eng": 3, "ci": 3}, previous, shift=1)
    assert (one["ceilings"], one["shift"]) == ({"plan": 0, "ci": 5, "eng": 1}, 1)
    assert one["reason"].endswith("; lane shift 1 from eng to ci.")


def test_a_lane_missing_from_live_and_demand_has_no_floor():
    previous = {"ceilings": {"plan": 0, "ci": 0, "eng": 1}, "pending_raise": {"target": None, "ticks": 0}}
    decision = calculate({"ci": 0}, {"claude": 1}, 10, {}, previous, shift=1)
    assert (decision["ceilings"], decision["shift"]) == ({"plan": 0, "ci": 1, "eng": 0}, 1)


def test_a_clamped_shift_leaves_the_reason_unchanged():
    previous = {"ceilings": {"plan": 0, "ci": 1, "eng": 5}, "pending_raise": {"target": None, "ticks": 0}}
    args = ({"eng": 3, "ci": 0}, {"claude": 4}, 10, {"eng": 0, "ci": 3}, previous)
    assert calculate(*args, shift=9)["reason"] == calculate(*args)["reason"]


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


@pytest.fixture
def shipped(monkeypatch):
    from scripts.swarm import metrics

    rows = []
    monkeypatch.setattr(metrics, "record", lambda table, found, now_ms: rows.append((table, found, now_ms)) or [])
    return rows


def steps(store, monkeypatch, tasks, record=None, spent=0):
    ready = {"eng": [{"id": "r1"}], "ci": [{"id": "r2"}, {"id": "r3"}], "plan": []}
    given = {"tasks": [{"id": "given"}]}
    asked = []
    monkeypatch.setattr(capacity, "ready_work", lambda *args: asked.append(args) or (tasks, ready))
    counted = []
    monkeypatch.setattr(
        capacity, "spawn_counter", lambda saved, now_ms: lambda since: counted.append((saved, since, now_ms)) or spent
    )
    host = {"host": {"room": 4, "granted_at": NOW - 5}}
    store.redis.set(store.key(SLUG, "quota-capacity"), json.dumps(host if record is None else record))
    actions = []
    for n in range(3):
        report(store, "ci", NOW + n * 60_000)
        actions += lane_split.step(SLUG, store.config(SLUG), store, given, NOW + n * 60_000)
    assert asked == [(SLUG, store, given)]
    assert counted in ([], [(store, NOW - 5, NOW + 120_000)])
    return actions


def test_the_tick_step_moves_a_seat_and_ships_one_dispatch_row(store, home, shipped, monkeypatch):
    from scripts.swarm import dispatcher

    actions = steps(store, monkeypatch, {})
    assert caps(store) == (3, 2)
    ((table, rows, at),) = shipped
    assert (table, at) == (dispatcher.TABLE, NOW + 120_000)
    ((row,),) = [rows]
    assert {k: row[k] for k in ("event_id", "ledger", "ts_ms", "rule", "mode", "action", "task")} == {
        "event_id": f"dispatch:{SLUG}:lane-split:{NOW + 120_000}:0",
        "ledger": SLUG,
        "ts_ms": NOW + 120_000,
        "rule": "lane-split",
        "mode": "apply",
        "action": actions[0],
        "task": "",
    }
    (logged,) = gate_log.recent(SLUG, None, home)
    assert logged["at"] == NOW + 120_000


def test_the_tick_step_counts_ready_work(store, home, shipped, monkeypatch):
    store.update(SLUG, max_eng=1)
    assert steps(store, monkeypatch, {}) == [
        lane_split.HELD.format(named="ci", ticks=3, reason="the eng lane keeps its last seat for ready work")
    ]


def test_the_tick_hands_its_ledger_state_and_clock_to_the_lane_split(monkeypatch):
    import fakeredis

    from scripts.swarm import tick
    from tests.swarm.test_tick import FakeLedger, FakeRuntime

    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(SwarmConfig(SLUG, "/repo", max_eng=0, max_ci=0))
    seen = []
    monkeypatch.setattr(lane_split, "step", lambda *a: seen.append((a[-2]["tasks"], a[-1])) or [])
    tick.tick(SLUG, saved, FakeLedger([{"id": "a"}]), FakeRuntime(), now_ms=4_321)
    assert [(tasks[0]["id"], at) for tasks, at in seen] == [("a", 4_321)]


def test_the_tick_step_counts_working_agents_as_live(store, home, shipped, monkeypatch):
    from scripts.swarm.store import AgentRecord

    store.update(SLUG, max_ci=4)
    for name, task_id in (("ci@x-1", "t2"), ("ci@x-2", "t3")):
        store.put_agent(SLUG, AgentRecord(name, "ci", task_id))
    working = {"t2": {"id": "t2", "state": "claimed"}, "t3": {"id": "t3", "state": "claimed"}}
    steps(store, monkeypatch, working)
    assert caps(store) == (3, 5)


def test_the_tick_step_skips_ended_agents_reads_host_room_and_ships_nothing_for_a_hold(
    store, home, shipped, monkeypatch
):
    from scripts.swarm.store import AgentRecord

    store.update(SLUG, max_ci=3)
    for name, task_id in (("ci@x-1", "t2"), ("ci@x-2", "t3")):
        store.put_agent(SLUG, AgentRecord(name, "ci", task_id))
    ended = {"t2": {"id": "t2", "state": "done"}, "t3": {"id": "t3", "state": "done"}}
    assert steps(store, monkeypatch, ended, spent=1) == [
        lane_split.HELD.format(named="ci", ticks=3, reason="the ci lane would pass host room 3")
    ]
    assert (caps(store), shipped) == ((4, 3), [])


def test_the_tick_step_without_a_host_reading_moves_without_a_room_check(store, home, shipped, monkeypatch):
    store.update(SLUG, max_ci=9)
    steps(store, monkeypatch, {}, record={})
    assert caps(store) == (3, 10)
    (row,) = gate_log.recent(SLUG, None, home)
    assert row["reason"].endswith(" Host room unknown.")


def test_the_tick_runs_the_lane_split_after_the_rank_step_and_before_spawns():
    import inspect

    from scripts.swarm import tick

    placing = inspect.getsource(tick.tick)
    placing = placing[placing.index("with PLACING:") :]
    assert placing.index("dispatcher.rank_pass") < placing.index("lane_split.step") < placing.index("_spawn,")
