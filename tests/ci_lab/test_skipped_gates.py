import json
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts.ci_lab import scheduler
from scripts.ci_lab.units import Priority
from tests.ci_lab.test_scheduler import BASE, DELIVERY, HEAD_A, TREE, Port, Run, _commit, _suite, _tree, unit
from tests.ci_lab.test_scheduler import lab as lab
from tests.ci_lab.test_scheduler import staticless as staticless


@pytest.mark.feature("lab-skipped-authoritative-gates")
def test_a_pull_request_frame_names_each_authoritative_gate_it_grades_with_no_unit() -> None:
    gates = {"lint": (unit(1),), "unit": (unit(2),)}
    authoritative = ("lint", "docs", "unit", "unit-selected")
    for tier in (Priority.HOTFIX, Priority.PULL_REQUEST):
        framed = scheduler.frame(tier, TREE, BASE, gates, authoritative)
        assert framed.idle == frozenset({"docs", "unit", "unit-selected"})
        assert framed.gates == scheduler.frame(tier, TREE, BASE, gates).gates
        assert scheduler.frame(tier, TREE, BASE, gates).idle == frozenset()
    for tier in (Priority.QUEUE, Priority.PROMOTION, Priority.BACKFILL):
        assert scheduler.frame(tier, TREE, BASE, gates, authoritative).idle == frozenset()


@pytest.mark.feature("lab-pull-request-reusable-job-units")
def test_a_pull_request_frame_grades_each_reusable_workflow_job_under_the_job_that_calls_it() -> None:
    gates = {
        "lint": (unit(1),),
        "semgrep/scan": (unit(2),),
        "semgrep/feature-tests": (unit(3),),
        "semgrep/charts": (unit(4),),
    }
    for tier in (Priority.HOTFIX, Priority.PULL_REQUEST):
        framed = scheduler.frame(tier, TREE, BASE, gates, ("semgrep",))
        assert framed.gates["semgrep"] == (unit(2), unit(3), unit(4))
        assert framed.gates["lint"] == (unit(1),)
        assert set(framed.gates) == set(scheduler.PULL_REQUIRED)
        assert framed.idle == frozenset()
    for tier in (Priority.PROMOTION, Priority.BACKFILL):
        assert scheduler.frame(tier, TREE, BASE, gates).gates == gates


@pytest.mark.feature("lab-skipped-authoritative-gates")
def test_a_pull_request_head_posts_each_authoritative_gate_it_runs_no_unit_of_skipped_on_its_first_pass(
    lab: Callable[..., Run],
) -> None:
    run = lab({HEAD_A: scheduler.frame(Priority.PULL_REQUEST, TREE, BASE, {"lint": (unit(1),)}, ("lint", "unit"))})
    run.push(HEAD_A, event="pull_request", action="synchronize")
    actions = run.tick(1000)
    assert run.grade(HEAD_A).idle == ["unit"]
    assert f"posted unit completed on {HEAD_A}" in actions
    assert run.lab.poster.moves(HEAD_A, "unit") == [("completed", "skipped")]
    assert run.lab.poster.moves(HEAD_A, "lint") == [("queued", None)]
    run.finish(run.lease()[0], "passed", {})
    run.tick(1100)
    assert run.lab.poster.moves(HEAD_A, "unit") == [("completed", "skipped")]
    assert run.lab.poster.moves(HEAD_A, "lint")[-1] == ("completed", "success")
    assert run.lab.poster.moves(HEAD_A, scheduler.REQUIRED)[-1] == ("completed", "success")
    grade = run.grade(HEAD_A)
    [skipped] = [post[4] for post in run.lab.poster.posts if post[1] == "unit"]
    assert skipped.units == ()
    assert skipped.summary == f"0 units of unit on {HEAD_A} against {BASE}"
    assert (skipped.title, skipped.run, skipped.grader) == ("pull_request grade completed", grade.run, BASE)


@pytest.mark.feature("lab-skipped-authoritative-gates")
def test_a_grade_ref_head_leaves_each_skipped_authoritative_gate_to_the_relay(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: scheduler.frame(Priority.PULL_REQUEST, TREE, BASE, {"lint": (unit(1),)}, ("unit",))})
    run.cache(unit(1))
    run.push(HEAD_A, lane="engineer-1", event="notice", ref="refs/grade/t1")
    actions = run.tick(1000)
    assert run.grade(HEAD_A).posted["unit"] == ["completed", "skipped"]
    assert f"left unit completed on {HEAD_A} to the relay" in actions
    assert run.lab.poster.posts == []


@pytest.mark.feature("lab-skipped-authoritative-gates")
@pytest.mark.usefixtures("staticless")
@pytest.mark.parametrize("tier", [Priority.PULL_REQUEST, Priority.HOTFIX])
def test_a_pull_request_or_hotfix_head_takes_its_authoritative_gates_from_its_base(
    tmp_path: Path, tier: Priority
) -> None:
    suite = _suite(tmp_path, {**DELIVERY["lab"], "authoritative": ["lint", "unit"]})
    widened = _commit(
        suite.repo, {"delivery.yaml": json.dumps({"lab": {**DELIVERY["lab"], "authoritative": ["docs"]}})}
    )
    found = Port().expander(tmp_path).expand(suite.request(tier, widened))
    assert found == scheduler.frame(tier, _tree(suite, widened), suite.base, {}, ("lint", "unit"))
    assert found.idle == frozenset({"lint", "unit"})
