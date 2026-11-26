"""The lab scheduler grades a push in the promotion class only on a named lane.

A push to dev travels with dev as its lane, so a newer push to dev replaces the one waiting and supersedes the one
being graded; a push to any other branch holds no head; and each pass retires the promotion work whose lane is its own
head, before it releases any waiting head.
"""

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts.ci_lab import scheduler, webhooks
from scripts.ci_lab.objects import LocalObjects
from scripts.ci_lab.retry import Policy
from scripts.ci_lab.units import Priority
from scripts.ci_lab.webhooks import envelope
from tests.ci_lab.test_scheduler import (
    ACTOR,
    BASE,
    HEAD_A,
    HEAD_B,
    REPO,
    Consumer,
    Delivery,
    Expander,
    Inbox,
    Lab,
    Run,
    fake_redis,
    plan,
    unit,
)

HEAD_C = "e" * 40
DEV_LANE = f"{REPO}|dev|{int(Priority.PROMOTION)}"


@pytest.fixture
def lab(tmp_path: Path) -> Callable[..., Run]:
    def make(plans: dict[str, scheduler.Plan], quiet_ms: int = 0) -> Run:
        state = Lab(fake_redis(), LocalObjects(tmp_path / "objects"), Inbox(), Expander(plans))
        return Run(state, state.build(quiet_ms, Policy(), None))

    return make


def _pushed(run: Run, head: str, ref: str = "refs/heads/dev") -> None:
    payload = {
        "ref": ref,
        "before": BASE,
        "after": head,
        "deleted": False,
        "repository": {"html_url": REPO},
        "sender": {"login": ACTOR},
    }
    deliveries = [Delivery(envelope(event)) for event in webhooks.head_events("push", payload)]
    run.lab.inbox.pending.extend(scheduler.BusInbox(Consumer([*deliveries])).heads())


def _swept(waiting: int, live: int, units: int) -> str:
    return (
        f"retired the promotion grades of pushes that named no branch: {waiting} waiting heads dropped, "
        f"{live} live grades retired, {units} pending units dropped"
    )


def _sweeps(actions: list[str]) -> list[str]:
    return [action for action in actions if "named no branch" in action]


def _runs(run: Run) -> set[str]:
    return {grade.run for grade in run.grades()}


@pytest.mark.feature("lab-promotion-grade-only-for-dev-push")
def test_a_newer_push_to_dev_replaces_the_push_waiting_on_the_dev_lane(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)}), HEAD_B: plan({"lint": (unit(2),)})}, quiet_ms=500)
    _pushed(run, HEAD_A)
    run.tick(1000)
    _pushed(run, HEAD_B)
    run.tick(1100)
    waiting = run.lab.client.hgetall(scheduler.WAITING)
    assert list(waiting) == [DEV_LANE]
    assert json.loads(waiting[DEV_LANE])["request"]["head"] == HEAD_B
    assert run.grades() == []


@pytest.mark.feature("lab-promotion-grade-only-for-dev-push")
def test_a_newer_push_to_dev_supersedes_the_dev_grade_before_it(lab: Callable[..., Run]) -> None:
    first, second = unit(1), unit(2)
    run = lab({HEAD_A: plan({"lint": (first,)}), HEAD_B: plan({"lint": (second,)})})
    _pushed(run, HEAD_A)
    run.tick(1000)
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_A, first)]
    _pushed(run, HEAD_B)
    run.tick(1100)
    assert run.grade(HEAD_B).lane == "dev"
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_B, second)]


@pytest.mark.feature("lab-promotion-grade-only-for-dev-push")
@pytest.mark.parametrize("ref", ["refs/heads/feature", "refs/heads/main", "refs/heads/feature/dev"])
def test_a_push_to_a_branch_other_than_dev_holds_no_head_and_starts_no_grade(lab: Callable[..., Run], ref: str) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)})})
    _pushed(run, HEAD_A, ref)
    run.tick(1000)
    assert run.lab.client.hgetall(scheduler.WAITING) == {}
    assert run.grades() == []
    assert run.engine.queue.pending() == []


@pytest.mark.feature("lab-promotion-grade-only-for-dev-push")
def test_a_waiting_push_that_named_no_branch_is_dropped_before_it_starts(lab: Callable[..., Run]) -> None:
    plans = {head: plan({"lint": (unit(number),)}) for number, head in enumerate((HEAD_A, HEAD_B, HEAD_C), 1)}
    run = lab(plans, quiet_ms=500)
    run.push(HEAD_A, lane=HEAD_A)
    run.push(HEAD_B, lane="dev")
    run.push(HEAD_C, lane=HEAD_C, event="pull_request", action="opened")
    assert _sweeps(run.tick(1000)) == []
    assert _sweeps(run.tick(1100)) == [_swept(1, 0, 0)]
    assert sorted(run.lab.client.hgetall(scheduler.WAITING)) == sorted(
        [DEV_LANE, f"{REPO}|{HEAD_C}|{int(Priority.PULL_REQUEST)}"]
    )
    assert _sweeps(run.tick(1600)) == []
    assert sorted(grade.head for grade in run.grades()) == [HEAD_B, HEAD_C]


@pytest.mark.feature("lab-promotion-grade-only-for-dev-push")
def test_a_live_promotion_grade_whose_lane_is_its_own_head_is_retired_on_the_next_pass(lab: Callable[..., Run]) -> None:
    first, second, third = unit(1), unit(2), unit(3)
    run = lab({HEAD_A: plan({"lint": (first,)}), HEAD_B: plan({"lint": (second,)}), HEAD_C: plan({"lint": (third,)})})
    run.push(HEAD_A, lane=HEAD_A)
    run.push(HEAD_B, lane="dev")
    run.push(HEAD_C, lane="feature", event="pull_request", action="opened")
    assert _sweeps(run.tick(1000)) == []
    kept = {run.grade(HEAD_B).run, run.grade(HEAD_C).run}
    assert len(_runs(run)) == 3
    assert _sweeps(run.tick(1100)) == [_swept(0, 1, 1)]
    assert sorted(run.engine.queue.pending()) == [scheduler.job_id(HEAD_B, second), scheduler.job_id(HEAD_C, third)]
    assert _runs(run) == kept
    assert _sweeps(run.tick(1200)) == []
    assert _runs(run) == kept


@pytest.mark.feature("lab-promotion-grade-only-for-dev-push")
def test_a_waiting_promotion_head_that_holds_a_grade_ref_is_kept(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)})}, quiet_ms=500)
    run.push(HEAD_A, lane=HEAD_A, ref=f"{scheduler.GRADE_REFS}{HEAD_A}")
    run.tick(1000)
    assert _sweeps(run.tick(1100)) == []
    assert list(run.lab.client.hgetall(scheduler.WAITING)) == [f"{REPO}|{HEAD_A}|{int(Priority.PROMOTION)}"]


@pytest.mark.feature("lab-promotion-grade-only-for-dev-push")
def test_a_retired_promotion_grade_keeps_the_units_of_a_head_another_live_grade_grades(
    lab: Callable[..., Run],
) -> None:
    first = unit(1)
    run = lab({HEAD_A: plan({"lint": (first,)})})
    run.push(HEAD_A, lane=HEAD_A)
    run.push(HEAD_A, lane="feature", event="pull_request", action="opened")
    run.tick(1000)
    tiers = sorted(grade.tier for grade in run.grades())
    assert tiers == [int(Priority.PULL_REQUEST), int(Priority.PROMOTION)]
    assert _sweeps(run.tick(1100)) == [_swept(0, 1, 0)]
    assert [grade.tier for grade in run.grades()] == [int(Priority.PULL_REQUEST)]
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_A, first)]


@pytest.mark.feature("lab-promotion-grade-only-for-dev-push")
def test_a_pull_request_grade_and_a_grade_ref_grade_whose_lane_is_their_head_are_kept(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)}), HEAD_B: plan({"lint": (unit(2),)})})
    run.push(HEAD_A, lane=HEAD_A, event="pull_request", action="opened")
    run.push(HEAD_B, lane=HEAD_B, ref=f"{scheduler.GRADE_REFS}{HEAD_B}")
    run.tick(1000)
    live = _runs(run)
    assert len(live) == 2
    assert _sweeps(run.tick(1100)) == []
    assert _runs(run) == live
    assert len(run.engine.queue.pending()) == 2


@pytest.mark.feature("lab-promotion-grade-only-for-dev-push")
def test_a_promotion_grade_on_the_dev_lane_is_kept(lab: Callable[..., Run]) -> None:
    first = unit(1)
    run = lab({HEAD_A: plan({"lint": (first,)})})
    run.push(HEAD_A, lane="dev")
    run.tick(1000)
    live = _runs(run)
    assert _sweeps(run.tick(1100)) == []
    assert _runs(run) == live
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_A, first)]


@pytest.mark.feature("lab-promotion-grade-only-for-dev-push")
def test_a_live_promotion_grade_that_lost_its_document_is_not_read_as_a_push_with_no_branch(
    lab: Callable[..., Run],
) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)})})
    run.push(HEAD_A, lane="dev")
    run.tick(1000)
    run.tick(1050)
    live = run.grade(HEAD_A).run
    assert live in run.lab.client.hgetall(scheduler.ORIGINS)
    run.lab.client.delete(scheduler.GRADES + live)
    assert _sweeps(run.tick(1100)) == []
    assert run.grades() == []
