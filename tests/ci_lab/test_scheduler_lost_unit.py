"""A unit the lab lost leaves its gate without a verdict: the fold, the check text, the fresh job a new grade gets and
the shadow report's reading of a neutral lab check."""

from collections.abc import Callable
from typing import Any

import pytest

from scripts.ci_lab import outcomes, scheduler, shadow_report
from scripts.ci_lab.checks import Output
from scripts.ci_lab.retry import Policy
from scripts.ci_lab.units import Unit
from tests.ci_lab.test_scheduler import BASE, HEAD_A, HEAD_B, Run, pull_plan, ref, unit
from tests.ci_lab.test_scheduler import lab as _lab

lab = _lab
APP = "agentihooks-lab"
NO_RETRY = Policy(lost=0)


def opened(lab: Callable[..., Run], gates: dict[str, tuple[Unit, ...]], head: str = HEAD_A) -> Run:
    """Return a lab whose pull request head holds the gates, graded up to its first pass."""
    run = lab({head: pull_plan(gates)}, retries=NO_RETRY)
    run.push(head, event="pull_request", action="synchronize")
    run.tick(1000)
    return run


def shown(run: Run, gate: str, head: str = HEAD_A) -> Output:
    """Return the output of the last check run posted for the gate on the head."""
    return [post[4] for post in run.lab.poster.posts if post[:2] == (head, gate)][-1]


def last(run: Run, gate: str, head: str = HEAD_A) -> tuple[str, str | None]:
    """Return the last move posted for the gate on the head."""
    return run.lab.poster.moves(head, gate)[-1]


def check_run(conclusion: str) -> dict[str, Any]:
    """Return one completed lab shadow lint check run as the GitHub API lists it."""
    return {
        "id": 2,
        "name": "lab-shadow/lint",
        "status": "completed",
        "conclusion": conclusion,
        "completed_at": "2026-11-18T00:05:00Z",
        "app": {"slug": APP},
    }


@pytest.mark.feature("lab-lost-unit-leaves-gate-neutral")
def test_a_gate_that_lost_a_unit_and_failed_none_concludes_neutral_and_so_does_the_required_check(
    lab: Callable[..., Run],
) -> None:
    lost, passed, other = unit(1), unit(2), unit(3)
    run = opened(lab, {"lint": (lost, passed), "semgrep": (other,)})
    run.lease(ttl_ms=100)
    for grant in run.lease(slots=2):
        run.finish(grant, outcomes.PASSED, {})

    actions = run.tick(1100)

    grade = run.grade(HEAD_A)
    assert grade.units[ref(lost)].outcome == outcomes.LOST
    assert last(run, "lint") == ("completed", "neutral")
    assert last(run, "semgrep") == ("completed", "success")
    assert last(run, scheduler.REQUIRED) == ("completed", "neutral")
    assert grade.conclusion == "neutral"
    assert f"completed {grade.run} neutral" in actions
    assert run.store.get(lost.kind, lost.key) is None


@pytest.mark.feature("lab-lost-unit-leaves-gate-neutral")
@pytest.mark.parametrize("outcome", [outcomes.CRASH, outcomes.CANNOT_RUN])
def test_a_unit_its_worker_reports_as_crashed_or_unable_to_run_is_lost_like_an_expired_lease(
    lab: Callable[..., Run], outcome: str
) -> None:
    item = unit(1)
    run = opened(lab, {"lint": (item,)})

    run.finish(run.lease()[0], outcome)
    run.tick(1100)

    assert last(run, "lint") == ("completed", "neutral")
    assert run.grade(HEAD_A).conclusion == "neutral"


@pytest.mark.feature("lab-lost-unit-leaves-gate-neutral")
def test_a_unit_whose_job_vanished_before_it_was_collected_is_lost(lab: Callable[..., Run]) -> None:
    item = unit(1)
    run = opened(lab, {"lint": (item,)})

    run.engine.queue.drop(HEAD_A)
    run.tick(1100)

    assert run.grade(HEAD_A).units[ref(item)].outcome == outcomes.DROPPED
    assert last(run, "lint") == ("completed", "neutral")


@pytest.mark.feature("lab-lost-unit-leaves-gate-neutral")
def test_a_failed_unit_keeps_its_gate_failed_whatever_else_the_gate_lost(lab: Callable[..., Run]) -> None:
    lost, failed = unit(1), unit(2)
    run = opened(lab, {"lint": (lost, failed)})
    run.lease(ttl_ms=100)
    run.finish(run.lease()[0], outcomes.FAILED, {"seconds": 1.0})

    run.tick(1100)

    assert last(run, "lint") == ("completed", "failure")
    assert last(run, scheduler.REQUIRED) == ("completed", "failure")
    assert run.grade(HEAD_A).conclusion == "failure"


@pytest.mark.feature("lab-lost-unit-leaves-gate-neutral")
def test_a_failed_gate_keeps_the_required_check_failed_beside_a_gate_that_only_lost_a_unit(
    lab: Callable[..., Run],
) -> None:
    lost, failed = unit(1), unit(2)
    run = opened(lab, {"lint": (lost,), "semgrep": (failed,)})
    run.lease(ttl_ms=100)
    run.finish(run.lease()[0], outcomes.FAILED, {"seconds": 1.0})

    run.tick(1100)

    assert last(run, "lint") == ("completed", "neutral")
    assert last(run, "semgrep") == ("completed", "failure")
    assert last(run, scheduler.REQUIRED) == ("completed", "failure")
    assert run.grade(HEAD_A).conclusion == "failure"


@pytest.mark.feature("lab-lost-unit-leaves-gate-neutral")
def test_the_checks_of_a_grade_that_lost_units_name_how_many_their_keys_and_the_rerequest(
    lab: Callable[..., Run],
) -> None:
    first, second, passed, other = unit(1), unit(2), unit(3), unit(4)
    run = opened(lab, {"lint": (first, second, passed), "semgrep": (other,)})
    run.lease(slots=2, ttl_ms=100)
    for grant in run.lease(slots=2):
        run.finish(grant, outcomes.PASSED, {})

    run.tick(1100)

    note = (
        f"2 units ended without a verdict, lost by the lab: {first.key}, {second.key}. "
        "A rerequest of this check grades them again."
    )
    assert shown(run, "lint").summary == f"3 units of lint on {HEAD_A} against {BASE}\n{note}"
    assert shown(run, scheduler.REQUIRED).summary == f"4 units of required on {HEAD_A} against {BASE}\n{note}"
    assert shown(run, "semgrep").summary == f"1 units of semgrep on {HEAD_A} against {BASE}"


@pytest.mark.feature("lab-lost-unit-leaves-gate-neutral")
def test_a_rerequested_grade_runs_a_lost_unit_again_and_its_pass_turns_the_gate_green(
    lab: Callable[..., Run],
) -> None:
    item = unit(1)
    run = opened(lab, {"lint": (item,)})
    run.lease(ttl_ms=100)
    run.tick(1100)
    first = run.grade(HEAD_A)
    assert first.conclusion == "neutral"
    job = scheduler.job_id(HEAD_A, item)

    run.push(HEAD_A, event="check_run", action="rerequested")
    run.tick(2000)

    record = run.engine.queue.job(job)
    assert record is not None
    assert (record.state, record.attempts, record.outcome) == ("pending", 0, "")
    [grant] = run.lease()
    assert grant.job == job
    run.finish(grant, outcomes.PASSED, {"seconds": 1.0})
    run.tick(2100)
    again = run.engine.grade(f"{first.run}-2000")
    kept = run.engine.grade(first.run)
    assert again is not None and kept is not None
    assert again.units[ref(item)].outcome == outcomes.PASSED
    assert (again.conclusion, kept.conclusion) == ("success", "neutral")
    assert last(run, "lint") == ("completed", "success")
    assert last(run, scheduler.REQUIRED) == ("completed", "success")


@pytest.mark.feature("lab-lost-unit-leaves-gate-neutral")
def test_a_grade_waiting_on_a_job_lost_in_the_pass_of_a_rerequest_reads_the_fresh_run_and_never_the_loss(
    lab: Callable[..., Run],
) -> None:
    item = unit(1)
    run = opened(lab, {"lint": (item,)})
    run.lease(ttl_ms=100)
    first = run.grade(HEAD_A)
    job = scheduler.job_id(HEAD_A, item)
    run.push(HEAD_A, event="check_run", action="rerequested")

    actions = run.tick(1100)

    assert f"reaped {job} lost" in actions
    waiting, again = run.engine.grade(first.run), run.engine.grade(f"{first.run}-1100")
    assert waiting is not None and again is not None
    assert (waiting.units[ref(item)].outcome, again.units[ref(item)].outcome) == ("", "")
    [grant] = run.lease()
    assert grant.job == job
    run.finish(grant, outcomes.PASSED, {"seconds": 1.0})
    run.tick(1200)
    waited, graded = run.engine.grade(first.run), run.engine.grade(f"{first.run}-1100")
    assert waited is not None and graded is not None
    assert (waited.conclusion, graded.conclusion) == ("success", "success")


def test_a_sibling_head_s_grade_completes_in_the_pass_that_concludes_a_grade_with_a_lost_unit(
    lab: Callable[..., Run],
) -> None:
    lost, passed = unit(1), unit(2)
    run = lab({HEAD_A: pull_plan({"lint": (lost,)}), HEAD_B: pull_plan({"lint": (passed,)})}, retries=NO_RETRY)
    run.push(HEAD_A, event="pull_request", action="synchronize")
    run.push(HEAD_B, lane="other", event="pull_request", action="synchronize")
    run.tick(1000)
    run.lease(ttl_ms=100)
    run.finish(run.lease()[0], outcomes.PASSED, {})

    run.tick(1100)

    assert last(run, scheduler.REQUIRED, HEAD_B) == ("completed", "success")
    assert last(run, scheduler.REQUIRED, HEAD_A)[0] == "completed"
    assert run.lab.client.smembers(scheduler.LIVE) == set()


def test_a_superseded_grade_is_cancelled_whatever_its_units_lost(lab: Callable[..., Run]) -> None:
    lost, fresh = unit(1), unit(2)
    run = lab({HEAD_A: pull_plan({"lint": (lost,)}), HEAD_B: pull_plan({"lint": (fresh,)})}, retries=NO_RETRY)
    run.push(HEAD_A, event="pull_request", action="synchronize")
    run.tick(1000)
    run.lease(ttl_ms=100)
    run.push(HEAD_B, event="pull_request", action="synchronize")

    run.tick(1100)

    assert run.grade(HEAD_A).units[ref(lost)].outcome == outcomes.LOST
    assert last(run, "lint") == ("completed", "cancelled")
    assert last(run, scheduler.REQUIRED) == ("completed", "cancelled")
    assert shown(run, "lint").summary == f"1 units of lint on {HEAD_A} against {BASE}"


def test_the_shadow_report_reads_a_neutral_lab_check_as_no_verdict() -> None:
    assert shadow_report.lab([check_run("neutral")], "lint", APP) is None
    assert shadow_report.lab([check_run("failure")], "lint", APP) is not None
