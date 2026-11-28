"""A lane holding ready urgent tasks takes the next free quota seat and the first ceiling room before other lanes."""

from typing import Any

import pytest

from scripts.swarm import autoscale, capacity, plan_cap_floor
from scripts.swarm.store import AUTO_SCALING, AgentRecord, SwarmConfig
from tests.swarm.test_capacity import _store
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = pytest.mark.unit

NOW = 1_800_000_000_000
ROOM = {"room": 10, "reason": "", "limit": 10, "held": False, "granted_at": NOW}


def account(cap: int, sessions: int) -> capacity.Account:
    return capacity.Account("claude", "a", capacity._state(cap, None), sessions, 90, 90, cap)


def live(**lanes: int) -> list[AgentRecord]:
    return [AgentRecord(f"{lane}-{n}", lane, f"{lane}{n}") for lane, count in lanes.items() for n in range(count)]


def demand(counts: dict[str, int], urgent: dict[str, int]) -> autoscale.Demand:
    return autoscale.Demand({"eng": 0, "ci": 0, "plan": 0, "review": 0, **counts}, urgent)


def placed(decision: dict[str, Any]) -> dict[str, int]:
    return {lane: len(rows) for lane, rows in decision["placements"].items()}


def ceilings(live_agents: dict[str, int], seats: int, work: dict[str, int]) -> dict[str, int]:
    """The ceilings autoscale splits once a raise to the given seats has settled."""
    total = sum(live_agents.values()) + seats
    previous: autoscale.Decision = {
        "ceilings": {"plan": 0, "ci": 0, "eng": total},
        "pending_raise": {"target": None, "ticks": 0},
        "reason": "",
    }
    return autoscale.calculate(live_agents, {"claude": seats}, None, work, previous, now_ms=0)["ceilings"]


@pytest.mark.feature("swarm-urgent-lane-first")
def test_one_free_seat_goes_to_the_urgent_engineer_lane_before_emptier_ci_and_plan_lanes() -> None:
    config = SwarmConfig("sw", "/repo", max_eng=2, max_ci=1, max_plan=1)
    work = demand({"eng": 1, "ci": 1, "plan": 1}, {"eng": 1})
    decision = capacity.calculate(config, [account(cap=3, sessions=2)], live(eng=1), work)
    assert placed(decision) == {"eng": 1, "ci": 0, "plan": 0, "review": 0}


@pytest.mark.feature("swarm-urgent-lane-first")
def test_an_urgent_lane_takes_seats_up_to_its_urgent_tasks_then_the_fewest_effective_lane_takes_the_rest() -> None:
    config = SwarmConfig("sw", "/repo", max_eng=6, max_ci=5, max_plan=0)
    work = demand({"eng": 4, "ci": 4}, {"eng": 2})
    decision = capacity.calculate(config, [account(cap=6, sessions=2)], live(eng=2), work)
    assert placed(decision) == {"eng": 2, "ci": 2, "plan": 0, "review": 0}


@pytest.mark.feature("swarm-urgent-lane-first")
def test_urgent_work_in_two_lanes_shares_the_seats_by_fewest_effective_agents() -> None:
    config = SwarmConfig("sw", "/repo", max_eng=3, max_ci=3, max_plan=1)
    work = demand({"eng": 2, "ci": 2, "plan": 1}, {"eng": 2, "ci": 2})
    decision = capacity.calculate(config, [account(cap=3, sessions=1)], live(eng=1), work)
    assert placed(decision) == {"eng": 1, "ci": 1, "plan": 0, "review": 0}


@pytest.mark.feature("swarm-urgent-lane-first")
def test_scarce_seats_without_urgent_work_keep_the_fewest_effective_pick_after_each_placement() -> None:
    config = SwarmConfig("sw", "/repo", max_eng=6, max_ci=5, max_plan=0)
    work = {"eng": 4, "ci": 4, "plan": 0, "review": 0}
    decision = capacity.calculate(config, [account(cap=6, sessions=2)], live(eng=2), work)
    assert placed(decision) == {"eng": 1, "ci": 3, "plan": 0, "review": 0}


@pytest.mark.feature("swarm-urgent-lane-first")
@pytest.mark.parametrize(
    ("caps", "agents", "counts"),
    [
        ((6, 5, 0), {"eng": 2}, {"eng": 4, "ci": 4}),
        ((2, 1, 1), {"eng": 1}, {"eng": 1, "ci": 1, "plan": 1}),
        ((1, 1, 1), {}, {"eng": 1, "ci": 1, "plan": 1}),
    ],
    ids=["scarce eng and ci", "one seat three lanes", "tied empty lanes"],
)
def test_a_demand_without_urgent_work_places_exactly_as_its_plain_counts(
    caps: tuple[int, int, int], agents: dict[str, int], counts: dict[str, int]
) -> None:
    eng, ci, plan = caps
    config = SwarmConfig("sw", "/repo", max_eng=eng, max_ci=ci, max_plan=plan)
    rows = [account(cap=6, sessions=4)]
    plain = {"eng": 0, "ci": 0, "plan": 0, "review": 0, **counts}
    expected = capacity.calculate(config, rows, live(**agents), plain)
    assert capacity.calculate(config, rows, live(**agents), demand(counts, {})) == expected


@pytest.mark.feature("swarm-urgent-lane-first")
def test_urgent_engineer_work_takes_the_scarce_ceiling_room_before_ci_and_plan() -> None:
    work = demand({"plan": 1, "ci": 1, "eng": 3}, {"eng": 2})
    assert ceilings({"plan": 0, "ci": 0, "eng": 1}, 2, work) == {"plan": 0, "ci": 0, "eng": 3}


@pytest.mark.feature("swarm-urgent-lane-first")
def test_scarce_ceiling_room_without_urgent_work_goes_one_seat_each_to_plan_then_ci_then_eng() -> None:
    work = {"plan": 1, "ci": 1, "eng": 3, "review": 0}
    assert ceilings({"plan": 0, "ci": 0, "eng": 1}, 2, work) == {"plan": 1, "ci": 1, "eng": 1}


@pytest.mark.feature("swarm-urgent-lane-first")
def test_urgent_lanes_fill_their_ceiling_room_lowest_ceiling_first() -> None:
    work = demand({"plan": 1, "ci": 1, "eng": 3}, {"ci": 1, "eng": 3})
    assert ceilings({"plan": 0, "ci": 0, "eng": 2}, 2, work) == {"plan": 0, "ci": 1, "eng": 3}


@pytest.mark.feature("swarm-urgent-lane-first")
def test_a_lane_raised_for_urgent_work_leaves_the_one_seat_pass_to_the_other_lanes() -> None:
    work = demand({"plan": 1, "ci": 3, "eng": 1}, {"ci": 1})
    assert ceilings({"plan": 0, "ci": 0, "eng": 0}, 3, work) == {"plan": 1, "ci": 1, "eng": 1}


@pytest.mark.feature("swarm-urgent-lane-first")
@pytest.mark.parametrize(
    ("agents", "seats", "counts"),
    [
        ({"plan": 0, "ci": 0, "eng": 1}, 2, {"plan": 1, "ci": 1, "eng": 3}),
        ({"plan": 1, "ci": 2, "eng": 3}, 4, {"plan": 2, "ci": 3, "eng": 4}),
        ({"plan": 0, "ci": 3, "eng": 1}, 1, {"eng": 7}),
    ],
    ids=["scarce", "roomy", "one lane"],
)
def test_autoscale_without_urgent_work_splits_exactly_as_its_plain_counts(
    agents: dict[str, int], seats: int, counts: dict[str, int]
) -> None:
    plain = {"plan": 0, "ci": 0, "eng": 0, "review": 0, **counts}
    assert ceilings(agents, seats, demand(counts, {})) == ceilings(agents, seats, plain)


@pytest.mark.feature("swarm-urgent-lane-first")
def test_autoscaled_keeps_the_urgent_counts_and_the_plan_floor_spares_the_urgent_lane() -> None:
    config = SwarmConfig("sw", "/repo", max_eng=3, max_ci=0, max_plan=0, scaling=AUTO_SCALING)
    work = autoscale.Demand({"eng": 3, "ci": 1, "plan": 1}, {"eng": 2})
    inputs = capacity.ScaleInputs([account(cap=3, sessions=1)], live(eng=1), work, lambda: None, {}, now_ms=NOW)
    scaled, decision = capacity.autoscaled(config, inputs, ROOM)
    assert decision is not None
    assert decision["ceilings"] == {"plan": 1, "ci": 0, "eng": 3}
    assert (scaled.max_eng, scaled.max_ci, scaled.max_plan) == (3, 0, 1)


@pytest.mark.feature("swarm-urgent-lane-first")
def test_the_plan_floor_takes_its_seat_from_the_lane_without_urgent_work() -> None:
    work = demand({"plan": 1, "ci": 1, "eng": 3}, {"eng": 2})
    caps = plan_cap_floor.ceilings({"plan": 0, "ci": 1, "eng": 3}, work)
    assert caps == {"plan": 1, "ci": 0, "eng": 3}


@pytest.mark.feature("swarm-urgent-lane-first")
def test_the_plan_floor_cuts_neither_lane_when_both_hold_urgent_work() -> None:
    work = demand({"plan": 1, "ci": 2, "eng": 1}, {"eng": 1, "ci": 1})
    caps = plan_cap_floor.ceilings({"plan": 0, "ci": 2, "eng": 1}, work)
    assert caps == {"plan": 1, "ci": 2, "eng": 1}


@pytest.mark.feature("swarm-urgent-lane-first")
def test_the_plan_floor_without_urgent_work_takes_from_the_larger_lane_as_before() -> None:
    caps = {"plan": 0, "ci": 1, "eng": 3}
    work = {"plan": 1, "ci": 1, "eng": 3}
    assert plan_cap_floor.ceilings(caps, demand(work, {})) == plan_cap_floor.ceilings(caps, work)


@pytest.mark.feature("swarm-urgent-lane-first")
def test_ready_demand_counts_each_lanes_ready_tasks_and_the_urgent_ones_among_them() -> None:
    ready = {
        "eng": [{"id": "a", "rank": "urgent"}, {"id": "b"}, {"id": "c", "rank": "urgent"}],
        "ci": [{"id": "d", "rank": "high"}],
        "plan": [],
    }
    work = capacity.ready_demand(ready)
    assert work == {"eng": 3, "ci": 1, "plan": 0}
    assert autoscale.urgent_counts(work) == {"eng": 2, "ci": 0, "plan": 0}


@pytest.mark.feature("swarm-urgent-lane-first")
@pytest.mark.parametrize("plain", [{"eng": 2}, None], ids=["plain counts", "no demand"])
def test_a_plain_demand_carries_no_urgent_counts(plain: dict[str, int] | None) -> None:
    assert autoscale.urgent_counts(plain) == {}


class OneSeat(FakeRuntime):
    def quota_capacity(
        self,
        config: SwarmConfig,
        agents: list[AgentRecord],
        now: float,
        demand: dict[str, int] | None,
        requirements: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return capacity.calculate(config, [account(cap=3, sessions=2)], agents, demand)


def _ledger(rows: list[dict[str, Any]]) -> FakeLedger:
    ledger = FakeLedger(rows)
    ledger.state = lambda slug: {"tasks": list(ledger.rows.values())}
    ledger.comment = lambda *args, **kwargs: None
    return ledger


@pytest.mark.feature("swarm-urgent-lane-first")
def test_the_capacity_pass_seats_an_urgent_engineer_task_ahead_of_ready_ci_work() -> None:
    store = _store()
    config = SwarmConfig("sw", "/repo", max_eng=2, max_ci=1, max_plan=0)
    store.create(config)
    store.put_agent("sw", AgentRecord("engineer@x-0001", "eng", "held", state="working"))
    ledger = _ledger([{"id": "held"}, {"id": "fix", "rank": "urgent"}, {"id": "check", "lane": "ci"}])
    capacity.apply("sw", config, store, ledger, OneSeat(), 1000)
    assert capacity.read(store, "sw")["tasks"] == {"fix": "claude"}


@pytest.mark.feature("swarm-urgent-lane-first")
def test_live_inputs_carry_the_urgent_ready_counts_per_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store()
    store.create(SwarmConfig("sw", "/repo", max_eng=2, max_ci=1, max_plan=0))
    ledger = FakeLedger([{"id": "fix", "rank": "urgent"}, {"id": "next"}, {"id": "check", "lane": "ci"}])
    monkeypatch.setattr(capacity, "accounts", lambda env, now, refresh=True: [account(cap=6, sessions=0)])
    inputs = capacity.live_inputs("sw", store, ledger, {}, 5_000)
    assert inputs.demand == {"eng": 2, "ci": 1, "plan": 0, "review": 0}
    assert autoscale.urgent_counts(inputs.demand) == {"eng": 1, "ci": 0, "plan": 0, "review": 0}
