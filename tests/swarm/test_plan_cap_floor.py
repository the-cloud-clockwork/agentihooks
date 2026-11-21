"""The plan lane cap stays at one or more while plan work is open, and a start or a scale up restores a stored zero."""

from typing import Any

import pytest

from scripts.swarm import capacity, dispatcher
from scripts.swarm.store import AUTO_SCALING, MANUAL_SCALING, AgentRecord, RedisStore, SwarmConfig
from tests.swarm.test_capacity import account
from tests.swarm.test_cli import env, run
from tests.swarm.test_tick import FakeRuntime

pytestmark = pytest.mark.unit

_fixture = env
NOW = 1_800_000_000_000
PLAN_READY = {"eng": 0, "ci": 0, "plan": 1}
NO_ROOM = {"room": 0, "reason": "", "limit": 0, "held": False, "granted_at": NOW}


def live(**lanes: int) -> list[AgentRecord]:
    """Running agents, the given count in each lane."""
    return [AgentRecord(f"{lane}-{n}", lane, f"{lane}{n}") for lane, count in lanes.items() for n in range(count)]


def scaled(
    agents: list[AgentRecord],
    demand: dict[str, int] | None,
    host: dict[str, Any] | None = NO_ROOM,
    scaling: str = AUTO_SCALING,
    **caps: int,
) -> tuple[SwarmConfig, dict[str, Any] | None, dict[str, int]]:
    """Autoscale a swarm with the given stored caps, then count the placements the quota decision makes per lane."""
    stored = {"max_eng": 0, "max_ci": 0, "max_plan": 0, **caps}
    config = SwarmConfig("sw", "/repo", scaling=scaling, **stored)
    inputs = capacity.ScaleInputs([account(cap=12, sessions=len(agents))], agents, demand, lambda: None, {}, now_ms=NOW)
    config, decision = capacity.autoscaled(config, inputs, host)
    placed = capacity.calculate(config, inputs.observations, agents, demand)
    return config, decision, {lane: len(rows) for lane, rows in placed["placements"].items()}


@pytest.mark.feature("swarm-plan-cap-floor")
def test_a_ready_plan_task_gets_a_planner_seat_under_a_stored_zero_manual_cap() -> None:
    config, decision, placed = scaled([], PLAN_READY, scaling=MANUAL_SCALING, max_eng=2, max_ci=1)
    assert (config.max_eng, config.max_ci, config.max_plan) == (2, 1, 1)
    assert decision is None
    assert placed["plan"] == 1


@pytest.mark.feature("swarm-plan-cap-floor")
@pytest.mark.parametrize("demand", [{"eng": 3, "ci": 0, "plan": 0}, None], ids=["no plan work", "no demand"])
def test_a_stored_zero_manual_cap_holds_while_no_plan_work_is_ready(demand: dict[str, int] | None) -> None:
    config, _, placed = scaled([], demand, scaling=MANUAL_SCALING, max_eng=2, max_ci=1)
    assert config.max_plan == 0
    assert placed["plan"] == 0


@pytest.mark.feature("swarm-plan-cap-floor")
def test_a_stored_manual_plan_cap_above_zero_is_kept() -> None:
    config, _, _ = scaled([], PLAN_READY, scaling=MANUAL_SCALING, max_eng=2, max_plan=3)
    assert config.max_plan == 3


@pytest.mark.feature("swarm-plan-cap-floor")
def test_a_ready_plan_task_gets_a_planner_seat_when_live_agents_fill_the_room() -> None:
    config, decision, placed = scaled(live(eng=6), PLAN_READY, max_eng=6)
    assert decision is not None
    assert decision["ceilings"] == {"plan": 1, "ci": 0, "eng": 5}
    assert config.max_plan == 1
    assert placed["plan"] == 1


@pytest.mark.feature("swarm-plan-cap-floor")
@pytest.mark.parametrize(
    ("agents", "caps", "ceilings"),
    [
        ({"eng": 2, "ci": 4}, {"max_eng": 2, "max_ci": 4}, {"plan": 1, "ci": 3, "eng": 2}),
        ({"eng": 3, "ci": 3}, {"max_eng": 3, "max_ci": 3}, {"plan": 1, "ci": 3, "eng": 2}),
        ({}, {}, {"plan": 1, "ci": 0, "eng": 0}),
    ],
    ids=["ci gives the seat", "a tie takes it from eng", "nothing to take"],
)
def test_the_plan_seat_comes_from_the_larger_lane_ceiling(
    agents: dict[str, int], caps: dict[str, int], ceilings: dict[str, int]
) -> None:
    _, decision, _ = scaled(live(**agents), PLAN_READY, **caps)
    assert decision is not None
    assert decision["ceilings"] == ceilings


@pytest.mark.feature("swarm-plan-cap-floor")
def test_the_ceilings_are_unchanged_without_ready_plan_work() -> None:
    _, decision, placed = scaled(live(eng=6), {"eng": 0, "ci": 0, "plan": 0}, max_eng=6)
    assert decision is not None
    assert decision["ceilings"] == {"plan": 0, "ci": 0, "eng": 6}
    assert placed["plan"] == 0


@pytest.mark.feature("swarm-plan-cap-floor")
def test_a_plan_ceiling_already_granted_is_unchanged() -> None:
    _, decision, _ = scaled([], {"eng": 3, "ci": 0, "plan": 1}, host=None)
    assert decision is not None
    assert decision["ceilings"] == {"plan": 1, "ci": 0, "eng": 1}


def ledger(*tasks: tuple[str, str]) -> dict[str, Any]:
    """A ledger doc holding one task per lane and state pair."""
    return {"tasks": [{"id": f"t{n}", "lane": lane, "state": state} for n, (lane, state) in enumerate(tasks)]}


@pytest.mark.feature("swarm-plan-cap-floor")
def test_the_lane_caps_keep_one_planner_while_a_plan_task_is_open() -> None:
    config = SwarmConfig("sw", "/repo", max_eng=4, max_ci=2, max_plan=0)
    assert dispatcher.lane_caps(config, ledger(("eng", "open"), ("plan", "open"))) == {"eng": 4, "ci": 2, "plan": 1}


@pytest.mark.feature("swarm-plan-cap-floor")
@pytest.mark.parametrize(
    "doc",
    [ledger(("plan", "claimed"), ("plan", "done")), ledger(("eng", "open")), None],
    ids=["plan work held or done", "no plan task", "no ledger"],
)
def test_the_lane_caps_keep_a_stored_zero_without_open_plan_work(doc: dict[str, Any] | None) -> None:
    config = SwarmConfig("sw", "/repo", max_eng=4, max_ci=2, max_plan=0)
    assert dispatcher.lane_caps(config, doc)["plan"] == 0


@pytest.mark.feature("swarm-plan-cap-floor")
def test_the_lane_caps_keep_a_stored_plan_cap_above_one() -> None:
    config = SwarmConfig("sw", "/repo", max_eng=4, max_ci=2, max_plan=3)
    assert dispatcher.lane_caps(config, ledger(("plan", "open")))["plan"] == 3


Env = tuple[RedisStore, Any, FakeRuntime]


@pytest.mark.feature("swarm-plan-cap-floor")
@pytest.mark.parametrize(("stored", "restored"), [("0", 1), ("3", 3)])
def test_start_restores_a_stored_zero_plan_cap_to_its_default(env: Env, stored: str, restored: int) -> None:
    store, _, _ = env
    run("sw", "create", "--repo", "/repo", "--max-eng-agents", "1", "--max-plan-agents", stored)
    assert run("sw", "start") == 0
    assert store.config("sw").max_plan == restored


@pytest.mark.feature("swarm-plan-cap-floor")
@pytest.mark.parametrize(
    ("pairs", "plan"),
    [
        (["max-eng-agents=4"], 1),
        (["max-ci-agents=3"], 1),
        (["max-eng-agents=4", "max-plan-agents=0"], 0),
        (["max-eng-agents=1"], 0),
        (["compact-limit=500"], 0),
    ],
    ids=["engineers up", "ci up", "plan named zero", "engineers down", "no cap change"],
)
def test_a_scale_up_restores_a_stored_zero_plan_cap(env: Env, pairs: list[str], plan: int) -> None:
    store, _, _ = env
    run("sw", "create", "--repo", "/repo", "--max-eng-agents", "2", "--max-ci-agents", "1", "--max-plan-agents", "0")
    assert run("sw", "set", *pairs) == 0
    assert store.config("sw").max_plan == plan


@pytest.mark.feature("swarm-plan-cap-floor")
def test_a_scale_up_keeps_a_stored_plan_cap_above_zero(env: Env) -> None:
    store, _, _ = env
    run("sw", "create", "--repo", "/repo", "--max-eng-agents", "2", "--max-plan-agents", "2")
    assert run("sw", "set", "max-eng-agents=5") == 0
    assert store.config("sw").max_plan == 2
