from scripts.swarm.autoscale import calculate


def test_more_accounts_raise_the_ceiling_only_on_the_third_tick():
    live = {"plan": 0, "ci": 0, "eng": 2}
    previous = {
        "ceilings": {"plan": 0, "ci": 0, "eng": 4},
        "pending_raise": {"target": None, "ticks": 0},
        "reason": "",
    }
    decisions = []
    for _ in range(3):
        previous = calculate(live, {"claude": 4, "codex": 2}, 10, {}, previous)
        decisions.append(previous)
    assert [d["ceilings"] for d in decisions] == [
        {"plan": 0, "ci": 0, "eng": 4},
        {"plan": 0, "ci": 0, "eng": 4},
        {"plan": 0, "ci": 0, "eng": 6},
    ]
    assert [d["pending_raise"] for d in decisions] == [
        {"target": 8, "ticks": 1},
        {"target": 8, "ticks": 2},
        {"target": 8, "ticks": 3},
    ]


def test_lane_ceiling_preserves_live_agents_and_splits_ready_demand_in_order():
    decision = calculate(
        {"plan": 1, "ci": 2, "eng": 3},
        {"claude": 2, "codex": 2},
        10,
        {"plan": 2, "ci": 3, "eng": 4},
        {"ceilings": {"plan": 1, "ci": 2, "eng": 10}, "pending_raise": {"target": None, "ticks": 0}, "reason": ""},
    )
    assert decision["ceilings"] == {"plan": 3, "ci": 4, "eng": 3}
    assert decision["pending_raise"] == {"target": None, "ticks": 0}


def test_total_ceiling_is_at_most_fifty():
    decision = calculate(
        {"plan": 1, "ci": 1, "eng": 46},
        {"claude": 30, "codex": 30},
        40,
        {},
        {"ceilings": {"plan": 1, "ci": 1, "eng": 48}, "pending_raise": {"target": None, "ticks": 0}, "reason": ""},
    )
    assert decision["ceilings"] == {"plan": 1, "ci": 1, "eng": 48}
    assert decision["pending_raise"] == {"target": None, "ticks": 0}


def test_reason_names_quota_seats_host_room_and_the_held_raise():
    decision = calculate({"eng": 2}, {"claude": 4, "codex": 2}, 10, {})
    assert decision["reason"] == (
        "Quota seats 6 (claude 4, codex 2); host room 10; ceiling 2 of 8; raise held at tick 1 of 3."
    )


def test_a_raise_climbs_two_seats_each_tick_after_three_ticks_then_clears():
    previous = None
    decisions = []
    for _ in range(6):
        previous = calculate({"eng": 2}, {"claude": 8}, 20, {}, previous)
        decisions.append(previous)
    assert [sum(d["ceilings"].values()) for d in decisions] == [2, 2, 4, 6, 8, 10]
    assert [d["pending_raise"]["ticks"] for d in decisions] == [1, 2, 3, 3, 3, 0]
    assert decisions[-1]["pending_raise"] == {"target": None, "ticks": 0}
    assert decisions[-1]["reason"] == "Quota seats 8 (claude 8); host room 20; ceiling 10 of 10."


def test_a_quota_reset_waits_three_ticks_after_an_immediate_drain():
    live = {"plan": 1, "ci": 1, "eng": 2}
    previous = {
        "ceilings": {"plan": 1, "ci": 1, "eng": 8},
        "pending_raise": {"target": 16, "ticks": 2},
        "reason": "",
    }
    drained = calculate(live, {"claude": 0, "codex": 0}, 20, {}, previous)
    assert drained["ceilings"] == live
    assert drained["pending_raise"] == {"target": None, "ticks": 0}
    assert drained["reason"] == "Quota seats 0 (claude 0, codex 0); host room 20; ceiling 4 of 4."
    decisions = []
    previous = drained
    for _ in range(3):
        previous = calculate(live, {"claude": 2, "codex": 0}, 20, {}, previous)
        decisions.append(previous)
    assert [d["ceilings"] for d in decisions] == [live, live, {"plan": 1, "ci": 1, "eng": 4}]
    assert decisions[-1]["pending_raise"] == {"target": None, "ticks": 0}


def test_host_room_caps_the_target_and_applies_a_lower_ceiling_at_once():
    decision = calculate(
        {"plan": 1, "ci": 1, "eng": 2},
        {"claude": 10, "codex": 10},
        1,
        {},
        {"ceilings": {"plan": 1, "ci": 1, "eng": 8}, "pending_raise": {"target": 20, "ticks": 2}, "reason": ""},
    )
    assert decision["ceilings"] == {"plan": 1, "ci": 1, "eng": 3}
    assert decision["pending_raise"] == {"target": None, "ticks": 0}
    assert decision["reason"] == "Quota seats 20 (claude 10, codex 10); host room 1; ceiling 5 of 5."


def test_a_changed_target_restarts_the_three_consecutive_ticks():
    previous = calculate({"eng": 2}, {"claude": 6}, 10, {})
    previous = calculate({"eng": 2}, {"claude": 6}, 10, {}, previous)
    changed = calculate({"eng": 2}, {"claude": 7}, 10, {}, previous)
    assert changed["ceilings"] == {"plan": 0, "ci": 0, "eng": 2}
    assert changed["pending_raise"] == {"target": 9, "ticks": 1}
    interrupted = calculate({"eng": 2}, {"claude": 0}, 10, {}, changed)
    assert interrupted["pending_raise"] == {"target": None, "ticks": 0}
    restarted = calculate({"eng": 2}, {"claude": 7}, 10, {}, interrupted)
    assert restarted["pending_raise"] == {"target": 9, "ticks": 1}


def test_unused_seats_go_to_engineering_after_planning_and_ci_demand():
    decision = calculate(
        {"plan": 1, "ci": 1, "eng": 2},
        {"claude": 8},
        10,
        {"plan": 1, "ci": 2, "eng": 1},
        {"ceilings": {"plan": 1, "ci": 1, "eng": 10}, "pending_raise": {"target": None, "ticks": 0}, "reason": ""},
    )
    assert decision["ceilings"] == {"plan": 2, "ci": 3, "eng": 7}


def test_ceilings_stay_nonnegative_when_live_agents_already_exceed_fifty():
    decision = calculate({"plan": 10, "ci": 20, "eng": 30}, {"claude": 10}, 10, {})
    assert decision["ceilings"] == {"plan": 10, "ci": 20, "eng": 20}


def test_a_confirmed_raise_reason_names_the_two_seat_limit():
    previous = {
        "ceilings": {"plan": 0, "ci": 0, "eng": 2},
        "pending_raise": {"target": 10, "ticks": 2},
        "reason": "",
    }
    decision = calculate({"eng": 2}, {"claude": 8}, 20, {}, previous)
    assert decision["reason"] == (
        "Quota seats 8 (claude 8); host room 20; ceiling 4 of 10; raise held by the two seat per tick limit."
    )
