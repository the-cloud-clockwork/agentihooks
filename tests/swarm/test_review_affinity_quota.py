"""Review lane harness affinity at placement and the reserved review share in autoscale."""

import json

import pytest

from scripts.swarm import autoscale, bottleneck_remedy, capacity, default_models, model_pick, runtime
from scripts.swarm.store import RedisStore, SwarmConfig, SwarmError
from tests.swarm.test_capacity import account
from tests.swarm.test_cli import env as env
from tests.swarm.test_cli import run

pytestmark = pytest.mark.unit

SLUG = "sw"
CODEX_MODEL = default_models.DEFAULTS["codex"]
REVIEWER = {"agent": "codex", "model": CODEX_MODEL, "effort": "xhigh"}
PIN = {"agent": "codex", "effort": "xhigh"}
CODEX_ONLY = ("codex",)
BOTH = ("claude", "codex")
IDLE: autoscale.PendingRaise = {"target": None, "ticks": 0}
OLD_LIVE = {"plan": 1, "ci": 2, "eng": 3}
OLD_DEMAND = {"plan": 2, "ci": 3, "eng": 9}
OLD_PREVIOUS: autoscale.Decision = {"ceilings": {"plan": 1, "ci": 2, "eng": 7}, "pending_raise": IDLE, "reason": ""}
OLD_DECISION = {
    "ceilings": {"plan": 3, "ci": 3, "eng": 4},
    "pending_raise": {"target": 11, "ticks": 1, "since": 0},
    "reason": "Quota seats 5 (claude 3, codex 2); host room 10; ceiling 10 of 11; raise held at tick 1 of 3.",
}
READINGS = {
    "accounts": [account(cap=6).__dict__, account("cx", harness="codex", cap=4).__dict__],
    "host": {"load1": 0, "cpus": 8, "available_mb": 64000, "agents": 0},
    "live": {"eng": 2, "ci": 1},
    "demand": {"eng": 4, "ci": 1, "plan": 0},
    "previous": {"autoscale": {"ceilings": {"eng": 2, "ci": 1, "plan": 0}, "pending_raise": {"target": 9, "ticks": 1}}},
}


def _previous(ceilings: dict[str, int]) -> autoscale.Decision:
    return {"ceilings": ceilings, "pending_raise": IDLE, "reason": ""}


def _shared(demand: dict[str, int], share: int, harnesses: tuple[str, ...] = CODEX_ONLY) -> autoscale.Decision:
    return autoscale.calculate(
        {"plan": 0, "ci": 0, "eng": 8, "review": 0},
        {"claude": 0, "codex": 4},
        2,
        demand,
        _previous({"plan": 0, "ci": 0, "eng": 10}),
        now_ms=0,
        review=autoscale.Review(share, harnesses),
    )


def _codex_full(harnesses: tuple[str, ...]) -> autoscale.Decision:
    return autoscale.calculate(
        {"plan": 0, "ci": 0, "eng": 4, "review": 0},
        {"claude": 6, "codex": 0},
        None,
        {"plan": 0, "ci": 0, "eng": 10, "review": 5},
        _previous({"plan": 0, "ci": 0, "eng": 10}),
        now_ms=0,
        review=autoscale.Review(50, harnesses),
    )


def _decide(
    live: dict[str, int], free: dict[str, int], host_room: int | None, demand: dict[str, int], share: int
) -> autoscale.Decision:
    return autoscale.calculate(
        live,
        free,
        host_room,
        demand,
        _previous({"plan": 0, "ci": 0, "eng": 10}),
        now_ms=0,
        review=autoscale.Review(share, CODEX_ONLY),
    )


def _review_placement(codex_sessions: int, lanes: dict[str, dict[str, str]]) -> dict:
    config = SwarmConfig(SLUG, "/repo", max_eng=0, max_ci=0, max_plan=0, max_review=2, lanes=lanes)
    accounts = [account("a", cap=3), account("cx", cap=3, sessions=codex_sessions, harness="codex")]
    return capacity.calculate(config, accounts, [], {"eng": 0, "ci": 0, "plan": 0, "review": 2})


def _autoscaled(config: SwarmConfig, demand: dict[str, int]) -> tuple[SwarmConfig, dict]:
    scaled, decision = capacity.autoscaled(config, capacity.fixture_inputs({**READINGS, "demand": demand}))
    assert decision is not None
    return scaled, decision


@pytest.fixture
def store() -> RedisStore:
    import fakeredis

    found = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    found.create(SwarmConfig(SLUG, "/repo", max_eng=2, max_ci=1, lanes={"review": PIN}))
    return found


@pytest.mark.feature("swarm-review-affinity-quota")
def test_ceilings_without_a_review_share_are_the_old_ceilings() -> None:
    decision = autoscale.calculate(OLD_LIVE, {"claude": 3, "codex": 2}, 10, OLD_DEMAND, OLD_PREVIOUS, now_ms=0)
    assert decision == OLD_DECISION


@pytest.mark.feature("swarm-review-affinity-quota")
def test_a_review_share_without_review_work_leaves_the_old_ceilings() -> None:
    decision = autoscale.calculate(
        {**OLD_LIVE, "review": 0},
        {"claude": 3, "codex": 2},
        10,
        {**OLD_DEMAND, "review": 0},
        OLD_PREVIOUS,
        now_ms=0,
        review=autoscale.Review(20, BOTH),
    )
    assert decision == OLD_DECISION


@pytest.mark.feature("swarm-review-affinity-quota")
def test_waiting_review_tasks_hold_the_review_share_before_the_other_lanes() -> None:
    demand = {"plan": 0, "ci": 0, "eng": 10, "review": 5}
    assert _shared(demand, 30)["ceilings"] == {"plan": 0, "ci": 0, "eng": 7, "review": 3}
    assert _shared(demand, 0)["ceilings"] == {"plan": 0, "ci": 0, "eng": 9, "review": 1}


@pytest.mark.feature("swarm-review-affinity-quota")
def test_no_waiting_review_task_releases_the_share_to_the_other_lanes() -> None:
    decision = _shared({"plan": 0, "ci": 0, "eng": 10, "review": 0}, 30)
    assert decision["ceilings"] == {"plan": 0, "ci": 0, "eng": 10}


@pytest.mark.feature("swarm-review-affinity-quota")
def test_live_reviewers_keep_their_seats_with_no_review_task_waiting() -> None:
    decision = autoscale.calculate(
        {"plan": 0, "ci": 0, "eng": 8, "review": 2},
        {"claude": 0, "codex": 4},
        0,
        {"plan": 0, "ci": 0, "eng": 10, "review": 0},
        _previous({"plan": 0, "ci": 0, "eng": 8, "review": 2}),
        now_ms=0,
        review=autoscale.Review(20, CODEX_ONLY),
    )
    assert decision["ceilings"] == {"plan": 0, "ci": 0, "eng": 8, "review": 2}


@pytest.mark.feature("swarm-review-affinity-quota")
def test_a_codex_only_review_lane_holds_no_claude_seat() -> None:
    assert _codex_full(CODEX_ONLY)["ceilings"] == {"plan": 0, "ci": 0, "eng": 10, "review": 0}
    assert _codex_full(BOTH)["ceilings"] == {"plan": 0, "ci": 0, "eng": 5, "review": 5}


@pytest.mark.feature("swarm-review-affinity-quota")
def test_the_share_never_holds_more_seats_than_review_tasks_wait() -> None:
    decision = autoscale.calculate(
        {"plan": 0, "ci": 0, "eng": 4, "review": 0},
        {"claude": 6, "codex": 0},
        None,
        {"plan": 0, "ci": 0, "eng": 10, "review": 1},
        _previous({"plan": 0, "ci": 0, "eng": 10}),
        now_ms=0,
        review=autoscale.Review(50, BOTH),
    )
    assert decision["ceilings"] == {"plan": 0, "ci": 0, "eng": 9, "review": 1}


@pytest.mark.feature("swarm-review-affinity-quota")
def test_the_reason_names_the_held_review_share() -> None:
    reason = _shared({"plan": 0, "ci": 0, "eng": 10, "review": 5}, 30)["reason"]
    assert reason == (
        "Quota seats 4 (claude 0, codex 4); host room 2; ceiling 10 of 10; "
        "review share holds 3 seats for 5 waiting review tasks."
    )


@pytest.mark.feature("swarm-review-affinity-quota")
def test_autoscaled_without_review_work_keeps_the_old_ceilings_and_the_review_cap() -> None:
    config = SwarmConfig(SLUG, "/repo", max_eng=1, max_ci=0, max_plan=5, max_review=2, lanes={"review": REVIEWER})
    scaled, decision = _autoscaled(config, {"eng": 4, "ci": 1, "plan": 0})
    assert decision["ceilings"] == {"plan": 0, "ci": 2, "eng": 3}
    assert (scaled.max_eng, scaled.max_ci, scaled.max_plan, scaled.max_review) == (3, 2, 0, 2)


@pytest.mark.feature("swarm-review-affinity-quota")
def test_autoscaled_holds_the_review_share_and_sizes_the_review_cap() -> None:
    config = SwarmConfig(SLUG, "/repo", max_eng=1, max_ci=0, max_plan=5, lanes={"review": REVIEWER}, review_share=30)
    scaled, decision = _autoscaled(config, {"eng": 4, "ci": 1, "plan": 0, "review": 3})
    assert decision["ceilings"] == {"plan": 0, "ci": 2, "eng": 2, "review": 2}
    assert (scaled.max_eng, scaled.max_ci, scaled.max_plan, scaled.max_review) == (2, 2, 0, 2)


@pytest.mark.feature("swarm-review-affinity-quota")
def test_a_codex_only_review_lane_is_placed_only_on_codex_while_claude_has_room() -> None:
    decision = _review_placement(codex_sessions=0, lanes={"review": REVIEWER})
    assert decision["allocation"]["review"] == {"claude": 0, "codex": 2}
    assert [slot["harness"] for slot in decision["placements"]["review"]] == ["codex", "codex"]
    assert decision["placeable"]["claude"] == 3


@pytest.mark.feature("swarm-review-affinity-quota")
def test_a_codex_only_review_lane_waits_while_codex_is_full() -> None:
    decision = _review_placement(codex_sessions=3, lanes={"review": REVIEWER})
    assert decision["allocation"]["review"] == {"claude": 0, "codex": 0}
    assert decision["placements"]["review"] == []
    assert decision["placeable"]["claude"] == 3


@pytest.mark.feature("swarm-review-affinity-quota")
def test_an_unpinned_review_lane_still_takes_claude_seats_while_codex_is_full() -> None:
    decision = _review_placement(codex_sessions=3, lanes={})
    assert decision["allocation"]["review"] == {"claude": 2, "codex": 0}
    assert [slot["harness"] for slot in decision["placements"]["review"]] == ["claude", "claude"]


@pytest.mark.feature("swarm-review-affinity-quota")
def test_the_review_lane_launches_with_its_codex_model_and_xhigh_effort() -> None:
    config = SwarmConfig(SLUG, "/repo", 0, 0, lanes={"review": REVIEWER})
    picked = runtime._lane_default("review", "codex", REVIEWER, config)
    assert picked == model_pick.ModelPick(CODEX_MODEL, "xhigh")


@pytest.mark.feature("swarm-review-affinity-quota")
def test_a_stuck_codex_never_moves_the_review_lane_pin(store: RedisStore) -> None:
    stage = bottleneck_remedy.Stage("launch_failure", 0.5, 0.25, 1, "task hours", CODEX_ONLY)
    moved = bottleneck_remedy._move(SLUG, store.config(SLUG), store, stage)
    assert moved == bottleneck_remedy.UNPINNED.format(harness="codex")
    assert store.config(SLUG).lanes["review"]["agent"] == "codex"


@pytest.mark.feature("swarm-review-affinity-quota")
def test_the_review_share_defaults_to_twenty_and_loads_for_existing_swarms(store: RedisStore) -> None:
    assert SwarmConfig(SLUG, "/repo", 0, 0).review_share == 20
    store.redis.hdel(store.key(SLUG, "config"), "review_share")
    assert store.config(SLUG).review_share == 20
    store.update(SLUG, review_share=35)
    assert store.config(SLUG).review_share == 35


@pytest.mark.feature("swarm-review-affinity-quota")
@pytest.mark.parametrize("share", [-1, 101])
def test_a_review_share_outside_zero_to_one_hundred_is_refused(share: int) -> None:
    with pytest.raises(SwarmError, match="^review share must be a whole number from 0 to 100$"):
        SwarmConfig(SLUG, "/repo", 0, 0, review_share=share)


@pytest.mark.feature("swarm-review-affinity-quota")
def test_a_review_share_of_zero_or_one_hundred_is_kept_and_one_more_is_refused() -> None:
    assert SwarmConfig(SLUG, "/repo", 0, 0, review_share=0).review_share == 0
    assert SwarmConfig(SLUG, "/repo", 0, 0, review_share=100).review_share == 100
    with pytest.raises(SwarmError, match="^review share must be a whole number from 0 to 100$"):
        SwarmConfig(SLUG, "/repo", 0, 0, review_share=101)


@pytest.mark.feature("swarm-review-affinity-quota")
def test_swarm_set_takes_the_review_share(env: tuple, capsys: pytest.CaptureFixture[str]) -> None:
    saved, _, _ = env
    run(SLUG, "create", "--repo", "/repo")
    capsys.readouterr()
    assert run(SLUG, "set", "review-share=35") == 0
    assert json.loads(capsys.readouterr().out)["review_share"] == 35
    assert saved.config(SLUG).review_share == 35
    assert run(SLUG, "set", "review-share=101") == 1
    assert saved.config(SLUG).review_share == 35


def test_readings_without_a_review_entry_count_no_reviewer_and_no_waiting_review_task() -> None:
    decision = autoscale.calculate(
        OLD_LIVE, {"claude": 3, "codex": 2}, 10, OLD_DEMAND, OLD_PREVIOUS, now_ms=0, review=autoscale.Review(20, BOTH)
    )
    assert decision == OLD_DECISION
    live = {"plan": 0, "ci": 0, "eng": 8}
    held = _decide(live, {"claude": 0, "codex": 4}, 2, {"plan": 0, "ci": 0, "eng": 10, "review": 5}, 30)
    assert held["ceilings"] == {"plan": 0, "ci": 0, "eng": 7, "review": 3}


def test_live_reviewers_without_a_review_demand_entry_keep_their_seats_and_name_no_share() -> None:
    live = {"plan": 0, "ci": 0, "eng": 8, "review": 2}
    decision = _decide(live, {"claude": 0, "codex": 4}, 0, {"plan": 0, "ci": 0, "eng": 10}, 20)
    assert decision == {
        "ceilings": {"plan": 0, "ci": 0, "eng": 8, "review": 2},
        "pending_raise": IDLE,
        "reason": "Quota seats 4 (claude 0, codex 4); host room 0; ceiling 10 of 10.",
    }


def test_a_pinned_harness_missing_from_the_free_seats_has_no_free_seat() -> None:
    live = {"plan": 0, "ci": 0, "eng": 8, "review": 0}
    decision = _decide(live, {"claude": 4}, 2, {"plan": 0, "ci": 0, "eng": 10, "review": 5}, 30)
    assert decision["ceilings"] == {"plan": 0, "ci": 0, "eng": 10, "review": 0}


def test_the_review_share_of_the_total_rounds_up_to_whole_seats() -> None:
    live = {"plan": 0, "ci": 0, "eng": 0, "review": 0}
    decision = _decide(live, {"claude": 0, "codex": 3}, None, {"plan": 0, "ci": 0, "eng": 3, "review": 3}, 67)
    assert decision["ceilings"] == {"plan": 0, "ci": 0, "eng": 0, "review": 3}


def test_held_review_seats_never_pass_the_total_when_the_host_has_no_room() -> None:
    live = {"plan": 0, "ci": 0, "eng": 0, "review": 2}
    decision = _decide(live, {"claude": 0, "codex": 4}, 0, {"plan": 0, "ci": 0, "eng": 0, "review": 3}, 20)
    assert decision["ceilings"] == {"plan": 0, "ci": 0, "eng": 0, "review": 2}


def test_a_zero_share_still_holds_one_seat_when_the_other_lanes_fill_the_room() -> None:
    live = {"plan": 0, "ci": 0, "eng": 9, "review": 0}
    decision = _decide(live, {"claude": 0, "codex": 1}, None, {"plan": 0, "ci": 0, "eng": 10, "review": 5}, 0)
    assert decision["ceilings"] == {"plan": 0, "ci": 0, "eng": 9, "review": 1}


def test_autoscaled_holds_review_seats_only_on_the_pinned_harness() -> None:
    config = SwarmConfig(SLUG, "/repo", max_eng=1, max_ci=0, max_plan=5, lanes={"review": REVIEWER}, review_share=30)
    accounts = [account(cap=6).__dict__, account("cx", harness="codex", cap=1).__dict__]
    readings = {**READINGS, "accounts": accounts, "demand": {"eng": 4, "ci": 1, "plan": 0, "review": 3}}
    scaled, decision = capacity.autoscaled(config, capacity.fixture_inputs(readings))
    assert decision is not None
    assert decision["ceilings"] == {"plan": 0, "ci": 2, "eng": 3, "review": 1}
    assert scaled.max_review == 1
