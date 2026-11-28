"""The lab grades the same test merge commit GitHub Actions grades for a pull request, issue 13890.

Before it expands a pull request grade, the scheduler resolves the test merge GitHub built for it and takes it
only when its second parent is the head. While GitHub has none yet, the head is held and tried again; at the
`UNMERGED_TRIES`th try the lab gives up and grades the bare head against its event base, as it always did.
"""

import json
import threading
from collections.abc import Callable
from concurrent.futures import wait as wait_futures
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from scripts.ci_lab import budget, merges, scheduler, static_gates
from scripts.ci_lab.checks import Output
from scripts.ci_lab.delivery import CLASSES
from scripts.ci_lab.digests import GradeInputs, StaticInputs, Toolchain, grade_key, static_key
from scripts.ci_lab.intake import GradeRequest
from scripts.ci_lab.objects import LocalObjects
from scripts.ci_lab.retry import Policy
from scripts.ci_lab.static_gates import StaticGate
from scripts.ci_lab.units import Priority
from scripts.ci_lab.webhooks import NOTICE, PUSHED, HeadEvent
from scripts.dep_manifest import _git
from tests.ci_lab.test_scheduler import (
    ACTOR,
    BASE,
    HEAD_A,
    HEAD_B,
    LOCK,
    REPO,
    TREE,
    Consumer,
    FakeChecks,
    Run,
    _blob,
    fake_redis,
    ref,
    unit,
)
from tests.ci_lab.test_scheduler import lab as lab
from tests.ci_lab.test_scheduler import origin as origin
from tests.ci_lab.test_scheduler_merge_base import _expander, _tree

MERGE = "9" * 40
PARENT = "8" * 40


def _push(run: Run, head: str, number: int, lane: str = "", event: str = "pull_request") -> None:
    event_obj = HeadEvent(event, REPO, head, BASE, ACTOR, "opened", lane or head, number)
    run.lab.inbox.pending.append(scheduler.Head(event_obj, lane or head))


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_resolved_merge_grades_the_merge_commit_with_the_head_on_every_check_run(lab: Callable[..., Run]) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates, merge_base=PARENT, graded_commit=MERGE)})
    run.engine.ports = replace(run.engine.ports, merges=Mock(return_value=(MERGE, PARENT)))
    _push(run, HEAD_A, 41)
    run.tick(1000)
    grade = run.grade(HEAD_A)
    assert grade.head == HEAD_A
    assert grade.graded_commit == MERGE
    assert grade.merge_base == PARENT
    assert grade.merge_fallback is False
    assert grade.run == grade_key(GradeInputs(REPO, HEAD_A, BASE, CLASSES[Priority.PULL_REQUEST], BASE, MERGE))
    [grant] = run.lease()
    assert grant.head == HEAD_A
    assert grant.graded_commit == MERGE
    assert run.lab.poster.posts
    assert all(post[0] == HEAD_A for post in run.lab.poster.posts)


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_the_summary_carries_the_graded_commit_key_of_a_resolved_grade_and_the_fallback_key_of_one(
    lab: Callable[..., Run],
) -> None:
    gates = {"lint": (unit(1),)}
    resolved = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates, merge_base=PARENT, graded_commit=MERGE)})
    resolved.engine.ports = replace(resolved.engine.ports, merges=Mock(return_value=(MERGE, PARENT)))
    _push(resolved, HEAD_A, 41)
    resolved.tick(1000)
    assert scheduler.summary(resolved.grade(HEAD_A))["graded_commit"] == MERGE
    assert "merge_fallback" not in scheduler.summary(resolved.grade(HEAD_A))

    fallen_back = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates)})
    fallen_back.engine.ports = replace(fallen_back.engine.ports, merges=Mock(return_value=None))
    _push(fallen_back, HEAD_A, 41)
    for _ in range(scheduler.UNMERGED_TRIES + 1):
        fallen_back.tick(1000)
    assert scheduler.summary(fallen_back.grade(HEAD_A))["merge_fallback"] is True
    assert "graded_commit" not in scheduler.summary(fallen_back.grade(HEAD_A))


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_pending_merge_resolution_holds_the_head_for_a_later_pass(lab: Callable[..., Run]) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates, merge_base=PARENT, graded_commit=MERGE)})
    port = Mock(side_effect=[merges.MergePending("still resolving"), (MERGE, PARENT)])
    run.engine.ports = replace(run.engine.ports, merges=port)
    _push(run, HEAD_A, 41)
    held = run.tick(1000)
    assert any(action.startswith(f"holding {HEAD_A}: still resolving") for action in held)
    assert run.grades() == []
    assert any(action.startswith("grading ") for action in run.tick(1000))
    assert run.grade(HEAD_A).graded_commit == MERGE
    assert port.call_count == 2


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_required_frame_regrade_of_a_resolved_merge_resolves_the_merge_again(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    gates = {"lint": (unit(1),)}
    other_merge, other_parent = "7" * 40, "6" * 40
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates, merge_base=PARENT, graded_commit=MERGE)})
    port = Mock(side_effect=[(MERGE, PARENT), (other_merge, other_parent)])
    run.engine.ports = replace(run.engine.ports, merges=port)
    _push(run, HEAD_A, 41)
    run.tick(1000)
    old = run.grade(HEAD_A)
    assert old.pull_number == 41
    monkeypatch.setattr(scheduler, "PULL_REQUIRED", (*scheduler.PULL_REQUIRED, "release-job"))
    actions = run.tick(2000)
    assert any(action.startswith("grading ") for action in actions)
    replacement = next(grade for grade in run.grades() if grade.run != old.run)
    assert replacement.pull_number == 41
    assert replacement.graded_commit == other_merge
    assert replacement.graded_commit != old.graded_commit
    assert [grade.run for grade in run.grades() if not grade.superseded] == [replacement.run]
    assert port.call_count == 2


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_required_frame_regrade_with_no_merge_yet_holds_then_falls_back_at_the_bound(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates, merge_base=PARENT, graded_commit=MERGE)})
    port = Mock(return_value=(MERGE, PARENT))
    run.engine.ports = replace(run.engine.ports, merges=port)
    _push(run, HEAD_A, 41)
    run.tick(1000)
    old = run.grade(HEAD_A)
    monkeypatch.setattr(scheduler, "PULL_REQUIRED", (*scheduler.PULL_REQUIRED, "release-job"))
    port.return_value = None
    for i in range(scheduler.UNMERGED_TRIES):
        actions = run.tick(2000 + i * 1000)
        assert any("holding the required-frame regrade" in action for action in actions)
        assert run.grade(HEAD_A).run == old.run
    actions = run.tick(2000 + scheduler.UNMERGED_TRIES * 1000)
    assert any(action.startswith("grading ") for action in actions)
    replacement = next(grade for grade in run.grades() if grade.run != old.run)
    assert replacement.merge_fallback is True
    assert replacement.graded_commit == ""
    assert [grade.run for grade in run.grades() if not grade.superseded] == [replacement.run]
    assert port.call_count == scheduler.UNMERGED_TRIES + 1


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_head_with_no_merge_yet_waits_out_the_window_then_falls_back_at_the_bound(lab: Callable[..., Run]) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates)})
    port = Mock(return_value=None)
    run.engine.ports = replace(run.engine.ports, merges=port)
    _push(run, HEAD_A, 41)
    for i in range(scheduler.UNMERGED_TRIES):
        actions = run.tick(1000)
        if i < scheduler.UNMERGED_TRIES - 1:
            assert any(action.startswith(f"holding {HEAD_A}: GitHub has resolved no merge") for action in actions)
        else:
            assert any(action.startswith(f"the lab gives up on a merge of {HEAD_A}") for action in actions)
        assert run.grades() == []
    actions = run.tick(1000)
    assert any(action.startswith("grading ") for action in actions)
    grade = run.grade(HEAD_A)
    assert grade.graded_commit == ""
    assert grade.merge_fallback is True
    assert port.call_count == scheduler.UNMERGED_TRIES


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_hotfix_head_with_no_merge_yet_spends_one_real_read_per_try(lab: Callable[..., Run]) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates)})
    run.lab.fixes.add(HEAD_A)
    port = Mock(return_value=None)
    run.engine.ports = replace(run.engine.ports, merges=port)
    _push(run, HEAD_A, 41)
    for i in range(scheduler.UNMERGED_TRIES):
        run.tick(1000 + i * 2000)
        assert run.grades() == []
    actions = run.tick(1000 + scheduler.UNMERGED_TRIES * 2000)
    assert any(action.startswith("grading ") for action in actions)
    assert run.grade(HEAD_A).merge_fallback is True
    assert port.call_count == scheduler.UNMERGED_TRIES


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_github_erroring_on_every_pull_request_read_also_falls_back_at_the_bound(lab: Callable[..., Run]) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates)})
    port = Mock(side_effect=merges.MergeUnanswered("GET pulls/41 answered 500"))
    run.engine.ports = replace(run.engine.ports, merges=port)
    _push(run, HEAD_A, 41)
    for _ in range(scheduler.UNMERGED_TRIES):
        run.tick(1000)
    actions = run.tick(1000)
    assert any(action.startswith("grading ") for action in actions)
    assert run.grade(HEAD_A).merge_fallback is True
    assert port.call_count == scheduler.UNMERGED_TRIES


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_merge_try_under_the_bound_restarts_its_waiting_window(lab: Callable[..., Run]) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates)}, quiet_ms=1000)
    port = Mock(return_value=None)
    run.engine.ports = replace(run.engine.ports, merges=port)
    _push(run, HEAD_A, 41)
    received = run.tick(1000)
    assert any(action.startswith(f"received {HEAD_A}") for action in received)
    assert port.call_count == 0
    actions = run.tick(2000)
    assert any(action.startswith(f"holding {HEAD_A}: GitHub has resolved no merge") for action in actions)
    assert port.call_count == 1
    held = run.tick(2500)
    assert not any(action.startswith(f"holding {HEAD_A}") for action in held)
    assert port.call_count == 1
    actions = run.tick(3001)
    assert any(action.startswith(f"holding {HEAD_A}: GitHub has resolved no merge") for action in actions)
    assert port.call_count == 2


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_merge_try_at_the_bound_gives_up_without_restarting_the_window(lab: Callable[..., Run]) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates)}, quiet_ms=1000)
    port = Mock(return_value=None)
    run.engine.ports = replace(run.engine.ports, merges=port)
    _push(run, HEAD_A, 41)
    run.tick(1000)
    now = 2000
    for _ in range(scheduler.UNMERGED_TRIES - 1):
        run.tick(now)
        now += 1000
    actions = run.tick(now)
    assert any(action.startswith(f"the lab gives up on a merge of {HEAD_A}") for action in actions)
    assert run.grades() == []
    gave_up_calls = port.call_count
    actions = run.tick(now + 1)
    assert any(action.startswith("grading ") for action in actions)
    assert run.grade(HEAD_A).merge_fallback is True
    assert port.call_count == gave_up_calls


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_refused_pull_request_read_holds_the_head_and_posts_no_failed_check(lab: Callable[..., Run]) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates)})
    port = Mock(side_effect=merges.MergeRefused("GET pulls/41 answered 404"))
    run.engine.ports = replace(run.engine.ports, merges=port)
    _push(run, HEAD_A, 41)
    actions = run.tick(1000)
    assert actions[-1].startswith(f"holding {HEAD_A}: GET pulls/41 answered 404")
    assert run.grades() == []
    assert run.lab.poster.posts == []


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_an_arbitrary_resolver_error_through_background_holds_the_head_and_posts_no_failed_check(
    lab: Callable[..., Run],
) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates)})
    release = threading.Event()

    def broken(repo: str, number: int, head: str) -> tuple[str, str] | None:
        release.wait(10.0)
        raise RuntimeError("boom")

    background = merges.Background(broken)
    run.engine.ports = replace(run.engine.ports, merges=background)
    _push(run, HEAD_A, 41)
    held = run.tick(1000)
    assert any(action.endswith("is still resolving") for action in held)
    release.set()
    wait_futures([future for _, future in background.resolutions.values()])
    actions = run.tick(1100)
    assert any(
        action.startswith(f"holding {HEAD_A}: the merge of {REPO}#41 for {HEAD_A} failed: boom") for action in actions
    )
    assert run.grades() == []
    assert run.lab.poster.posts == []


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_refused_pull_request_read_holds_the_required_frame_regrade(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates, merge_base=PARENT, graded_commit=MERGE)})
    port = Mock(return_value=(MERGE, PARENT))
    run.engine.ports = replace(run.engine.ports, merges=port)
    _push(run, HEAD_A, 41)
    run.tick(1000)
    old = run.grade(HEAD_A)
    before = list(run.lab.poster.posts)
    monkeypatch.setattr(scheduler, "PULL_REQUIRED", (*scheduler.PULL_REQUIRED, "release-job"))
    port.side_effect = merges.MergeRefused("GET pulls/41 answered 404")
    actions = run.tick(2000)
    assert any("holding the required-frame regrade" in action for action in actions)
    assert run.grade(HEAD_A).run == old.run
    assert run.lab.poster.posts == before


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_an_unanswered_pull_request_read_holds_the_required_frame_regrade(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates, merge_base=PARENT, graded_commit=MERGE)})
    port = Mock(return_value=(MERGE, PARENT))
    run.engine.ports = replace(run.engine.ports, merges=port)
    _push(run, HEAD_A, 41)
    run.tick(1000)
    old = run.grade(HEAD_A)
    before = list(run.lab.poster.posts)
    monkeypatch.setattr(scheduler, "PULL_REQUIRED", (*scheduler.PULL_REQUIRED, "release-job"))
    port.side_effect = merges.MergeUnanswered("GET pulls/41 answered 500")
    actions = run.tick(2000)
    assert any("holding the required-frame regrade" in action for action in actions)
    assert run.grade(HEAD_A).run == old.run
    assert run.lab.poster.posts == before


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_an_arbitrary_resolver_error_holds_the_required_frame_regrade(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates, merge_base=PARENT, graded_commit=MERGE)})
    port = Mock(return_value=(MERGE, PARENT))
    run.engine.ports = replace(run.engine.ports, merges=port)
    _push(run, HEAD_A, 41)
    run.tick(1000)
    old = run.grade(HEAD_A)
    before = list(run.lab.poster.posts)
    monkeypatch.setattr(scheduler, "PULL_REQUIRED", (*scheduler.PULL_REQUIRED, "release-job"))
    release = threading.Event()

    def broken(repo: str, number: int, head: str) -> tuple[str, str] | None:
        release.wait(10.0)
        raise RuntimeError("boom")

    background = merges.Background(broken)
    run.engine.ports = replace(run.engine.ports, merges=background)
    held = run.tick(2000)
    assert any(action.endswith("is still resolving") for action in held)
    assert run.grade(HEAD_A).run == old.run
    release.set()
    wait_futures([future for _, future in background.resolutions.values()])
    actions = run.tick(2100)
    assert any("failed: boom" in action for action in actions)
    assert run.grade(HEAD_A).run == old.run
    assert run.lab.poster.posts == before


@pytest.mark.feature("lab-grades-the-pull-request-merge")
@pytest.mark.parametrize("event", ["merge_group", "push", "notice"])
def test_a_merge_group_push_and_notice_grade_carries_no_pull_number_and_resolves_no_merge(
    lab: Callable[..., Run], event: str
) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates)})
    port = Mock(return_value=(MERGE, PARENT))
    run.engine.ports = replace(run.engine.ports, merges=port)
    _push(run, HEAD_A, 0, lane="dev", event=event)
    run.tick(1000)
    port.assert_not_called()
    assert run.grade(HEAD_A).graded_commit == ""


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_plant_grade_resolves_no_merge_even_if_it_somehow_carried_a_pull_number(lab: Callable[..., Run]) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates)})
    port = Mock(return_value=(MERGE, PARENT))
    run.engine.ports = replace(run.engine.ports, merges=port)
    _push(run, HEAD_A, 41, lane=HEAD_A, event="plant")
    run.tick(1000)
    port.assert_not_called()
    assert run.grade(HEAD_A).graded_commit == ""


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_superseding_a_lane_with_a_resolved_merge_still_drops_the_older_heads_units(lab: Callable[..., Run]) -> None:
    gates = {"lint": (unit(1),)}
    run = lab(
        {
            HEAD_A: scheduler.Plan(TREE, BASE, gates, merge_base=PARENT, graded_commit=MERGE),
            HEAD_B: scheduler.Plan(TREE, BASE, gates, merge_base=PARENT, graded_commit=MERGE),
        }
    )
    run.engine.ports = replace(run.engine.ports, merges=Mock(return_value=(MERGE, PARENT)))
    _push(run, HEAD_A, 41, lane="feature")
    run.tick(1000)
    _push(run, HEAD_B, 41, lane="feature")
    run.tick(1000)
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_B, unit(1))]
    assert run.grade(HEAD_B).graded_commit == MERGE


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_rerequested_check_run_after_dev_moved_grades_the_new_merge(lab: Callable[..., Run]) -> None:
    leased_unit, pending_unit = unit(1), unit(2)
    gates = {"lint": (leased_unit, pending_unit)}
    other_merge, other_parent = "7" * 40, "6" * 40
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates, merge_base=PARENT, graded_commit=MERGE)})
    port = Mock(side_effect=[(MERGE, PARENT), (other_merge, other_parent)])
    run.engine.ports = replace(run.engine.ports, merges=port)
    _push(run, HEAD_A, 41, lane="feature")
    run.tick(1000)
    old = run.grade(HEAD_A)
    assert old.graded_commit == MERGE
    [grant] = run.lease(slots=1)
    assert grant.unit == leased_unit
    job_pending = scheduler.job_id(HEAD_A, pending_unit)
    assert run.engine.queue.pending() == [job_pending]
    event = HeadEvent("check_run", REPO, HEAD_A, BASE, ACTOR, "rerequested", pull_number=41)
    run.lab.inbox.pending.append(scheduler.Head(event, HEAD_A))
    run.tick(1100)
    live = [grade for grade in run.grades() if not grade.superseded]
    assert [grade.head for grade in live] == [HEAD_A]
    assert live[0].graded_commit == other_merge
    assert live[0].run != old.run
    stale = run.engine.grade(old.run)
    assert stale is not None and stale.superseded is True
    carried = live[0].units[ref(leased_unit)]
    assert carried.job == grant.job
    assert carried.started is True
    assert carried.held is False
    assert carried.outcome == ""
    pending_slot = live[0].units[ref(pending_unit)]
    assert pending_slot.job == job_pending
    assert run.engine.queue.pending() == [job_pending]
    assert any(post[0] == HEAD_A for post in run.lab.poster.posts)
    assert port.call_count == 2
    assert [again.unit for again in run.lease(slots=2)] == [pending_unit]
    run.finish(grant, "passed", {})
    run.tick(1200)
    finished = run.engine.grade(live[0].run)
    assert finished is not None and finished.units[ref(leased_unit)].outcome == "passed"


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_the_evidence_of_a_resolved_grade_names_its_head_and_graded_commit(lab: Callable[..., Run]) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates, merge_base=PARENT, graded_commit=MERGE)})
    run.engine.ports = replace(run.engine.ports, merges=Mock(return_value=(MERGE, PARENT)))
    _push(run, HEAD_A, 41)
    run.tick(1000)
    state = run.grade(HEAD_A)
    found = scheduler._evidence(state)
    assert found.inputs.head == HEAD_A
    assert found.inputs.graded_commit == MERGE


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_pull_request_grade_with_no_pull_number_resolves_no_merge(lab: Callable[..., Run]) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates)})
    port = Mock(return_value=(MERGE, PARENT))
    run.engine.ports = replace(run.engine.ports, merges=port)
    run.push(HEAD_A, lane=HEAD_A, event="pull_request", action="opened")
    run.tick(1000)
    port.assert_not_called()
    assert run.grade(HEAD_A).graded_commit == ""


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_promotion_grade_resolves_no_merge_and_closes_with_no_fallback(lab: Callable[..., Run]) -> None:
    item = unit(1)
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, {"lint": (item,)})})
    port = Mock(return_value=(MERGE, PARENT))
    run.engine.ports = replace(run.engine.ports, merges=port)
    run.cache(item)
    run.push(HEAD_A)
    run.tick(1000)
    port.assert_not_called()
    grade = run.grade(HEAD_A)
    assert grade.graded_commit == ""
    assert grade.merge_fallback is False
    assert grade.conclusion == "success"
    assert "graded_commit" not in scheduler.summary(grade)
    assert "merge_fallback" not in scheduler.summary(grade)


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_an_unreachable_pull_request_head_is_retired_before_any_merge_is_asked(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates)})
    port = Mock(side_effect=AssertionError("the merges port must not be asked for an unreachable head"))
    run.engine.ports = replace(run.engine.ports, merges=port)
    monkeypatch.setattr(type(run.lab.expander), "reachable", lambda self, repo, head: False)
    _push(run, HEAD_A, 41)
    actions = run.tick(1000)
    assert actions[-1].startswith(f"retiring {HEAD_A}")
    port.assert_not_called()


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_pending_regrade_resolution_holds_and_counts_nothing(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates, merge_base=PARENT, graded_commit=MERGE)})
    port = Mock(return_value=(MERGE, PARENT))
    run.engine.ports = replace(run.engine.ports, merges=port)
    _push(run, HEAD_A, 41)
    run.tick(1000)
    monkeypatch.setattr(scheduler, "PULL_REQUIRED", (*scheduler.PULL_REQUIRED, "release-job"))
    port.side_effect = merges.MergePending("still resolving")
    actions = run.tick(2000)
    assert any(action.endswith("still resolving") for action in actions)
    assert run.grade(HEAD_A).unmerged == 0


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_regrades_spent_try_survives_a_new_scheduler_over_the_same_store(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates, merge_base=PARENT, graded_commit=MERGE)})
    port = Mock(return_value=(MERGE, PARENT))
    run.engine.ports = replace(run.engine.ports, merges=port)
    _push(run, HEAD_A, 41)
    run.tick(1000)
    monkeypatch.setattr(scheduler, "PULL_REQUIRED", (*scheduler.PULL_REQUIRED, "release-job"))
    port.side_effect = merges.MergeRefused("GET pulls/41 answered 404")
    run.tick(2000)
    assert run.grade(HEAD_A).unmerged == 1
    rebuilt = run.lab.build(0, Policy(), None)
    rebuilt.ports = replace(rebuilt.ports, merges=port)
    again = Run(run.lab, rebuilt)
    actions = again.tick(3000)
    assert any("holding the required-frame regrade" in action for action in actions)
    assert again.grade(HEAD_A).unmerged == 2


def _synthetic_merge(repo: Path, base: str, head: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """A commit whose tree is the head's own and whose parents are base then head, as GitHub builds a test merge."""
    for name, value in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@t")):
        monkeypatch.setenv(name, value)
        monkeypatch.setenv(name.replace("AUTHOR", "COMMITTER"), value)
    tree = _git(repo, "rev-parse", f"{head}^{{tree}}").strip()
    return _git(repo, "commit-tree", tree, "-p", base, "-p", head, "-m", "merge").strip()


@pytest.mark.feature("lab-grade-diffs-against-merge-base")
def test_expand_reads_the_tree_and_diffs_against_the_first_parent_of_a_resolved_merge(
    tmp_path: Path, origin: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, fork, head = origin
    merge = _synthetic_merge(repo, fork, head, monkeypatch)
    request = GradeRequest(f"file://{repo}", head, fork, Priority.PULL_REQUEST, ACTOR, False, graded_commit=merge)
    found = _expander(tmp_path, monkeypatch).expand(request)
    assert found.graded_commit == merge
    assert found.merge_base == fork
    assert found.tree == _tree(repo, head)


def test_expand_keys_a_gate_reading_commit_ids_on_the_lease_head_and_the_graded_commit(
    tmp_path: Path, origin: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, fork, head = origin
    merge = _synthetic_merge(repo, fork, head, monkeypatch)
    reading = StaticGate(
        "lint/trust",
        "lint",
        "trust",
        (),
        (),
        "",
        ("src",),
        ("src",),
        reads=(static_gates.COMMIT_ID_FIELDS[0], static_gates.COMMIT_ID_FIELDS[1]),
    )
    expander = _expander(tmp_path, monkeypatch)
    monkeypatch.setattr(scheduler, "discover", lambda workflow, registry, reusable: [reading])
    request = GradeRequest(f"file://{repo}", head, fork, Priority.PULL_REQUEST, ACTOR, False, graded_commit=merge)
    found = expander.expand(request)
    [found_unit] = found.gates["lint"]
    at_merge = {"src/a.py": _blob(repo, merge, "src/a.py")}
    at_fork = {"src/a.py": _blob(repo, fork, "src/a.py")}
    chain = Toolchain("3.12", {}, _blob(repo, merge, LOCK), _blob(repo, merge, "pyproject.toml"), "image", {})
    commit_ids = {"github.sha": merge, "github.event.pull_request.head.sha": head}
    expected = StaticInputs("lint/trust", at_merge, at_fork, fork, chain, commit_ids)
    assert found_unit.key == static_key(expected)
    assert found_unit.members == ("lint/trust",)


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_bundle_answers_a_bundle_of_the_commit_fetchable_once_its_two_parents_are_held(
    tmp_path: Path, origin: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, head = origin
    merge = _synthetic_merge(repo, base, head, monkeypatch)
    expander = _expander(tmp_path, monkeypatch)
    mirror = expander.mirror(f"file://{repo}")
    mirror.fetch(base)
    mirror.fetch(head)
    mirror.fetch(merge)
    found = expander.bundle(merge)
    assert found is not None
    target = tmp_path / "target.git"
    _git(tmp_path, "init", "-q", "--bare", str(target))
    _git(target, "fetch", "--quiet", str(mirror.store), f"{base}:refs/heads/base", f"{head}:refs/heads/head")
    bundle_file = tmp_path / "found.bundle"
    bundle_file.write_bytes(found)
    _git(target, "fetch", "--quiet", str(bundle_file), f"{merge}:refs/heads/merge")
    assert _git(target, "rev-parse", "refs/heads/merge").strip() == merge


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_bundle_leaves_no_ref_behind_after_it_answers(
    tmp_path: Path, origin: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, head = origin
    merge = _synthetic_merge(repo, base, head, monkeypatch)
    expander = _expander(tmp_path, monkeypatch)
    mirror = expander.mirror(f"file://{repo}")
    mirror.fetch(base)
    mirror.fetch(head)
    mirror.fetch(merge)
    assert expander.bundle(merge) is not None
    assert _git(mirror.store, "for-each-ref", "refs/lab-bundle/").strip() == ""


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_bundle_answers_two_requests_for_one_commit_at_once(
    tmp_path: Path, origin: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, head = origin
    merge = _synthetic_merge(repo, base, head, monkeypatch)
    expander = _expander(tmp_path, monkeypatch)
    mirror = expander.mirror(f"file://{repo}")
    mirror.fetch(base)
    mirror.fetch(head)
    mirror.fetch(merge)
    found: list[bytes | None] = [None, None]
    errors: list[BaseException] = []
    first_created = threading.Event()
    second_created = threading.Event()
    real_run = scheduler.subprocess.run

    def paced_run(argv: list[str], **kwargs: Any) -> scheduler.subprocess.CompletedProcess[bytes]:
        result = real_run(argv, **kwargs)
        if "update-ref" in argv and "-d" not in argv:
            if not first_created.is_set():
                first_created.set()
                second_created.wait(10.0)
            else:
                second_created.set()
        return result

    def ask(slot: int) -> None:
        try:
            found[slot] = expander.bundle(merge)
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(scheduler.subprocess, "run", paced_run)
    threads = [threading.Thread(target=ask, args=(0,)), threading.Thread(target=ask, args=(1,))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert found[0] is not None and found[1] is not None


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_bundle_answers_none_when_no_served_repository_store_holds_the_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expander = _expander(tmp_path, monkeypatch)
    assert expander.bundle("f" * 40) is None


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_build_wires_the_merges_and_bundle_ports_from_the_expander(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scheduler.MirrorExpander, "trusted", lambda self, repo: frozenset({repo}))
    monkeypatch.setattr(scheduler.MirrorExpander, "retries", lambda self, repo: Policy())
    monkeypatch.setattr(scheduler, "Checks", FakeChecks)
    environ = {
        scheduler.contract.GIT_MIRROR: str(tmp_path / "node"),
        scheduler.MIRROR_STORE: str(tmp_path / "stores"),
        scheduler.REPOSITORY: "repository",
        scheduler.IMAGE: "image",
    }
    client = fake_redis()
    objects = LocalObjects(tmp_path / "objects")
    spending = budget.Budget(client, lambda: 0, budget.Policy(100.0, {}))
    built = scheduler.build(environ, client, objects, Consumer([]), spending)
    expander = built.ports.expander
    assert isinstance(expander, scheduler.MirrorExpander)
    assert isinstance(built.ports.merges, merges.Background)
    assert isinstance(built.ports.merges.resolve, merges.Resolver)
    assert built.ports.merges.resolve.mirror == expander.mirror
    assert built.ports.bundle == expander.bundle


def _required_output(run: Run, head: str) -> Output:
    posts = [post[4] for post in run.lab.poster.posts if post[0] == head and post[1] == scheduler.REQUIRED]
    return posts[-1]


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_fallback_grades_required_check_names_that_github_held_no_merge(lab: Callable[..., Run]) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates)})
    run.engine.ports = replace(run.engine.ports, merges=Mock(return_value=None))
    run.cache(unit(1))
    _push(run, HEAD_A, 41)
    for _ in range(scheduler.UNMERGED_TRIES + 1):
        run.tick(1000)
    output = _required_output(run, HEAD_A)
    assert (
        f"The lab got no test merge of {HEAD_A} from GitHub within {scheduler.UNMERGED_TRIES} tries" in output.summary
    )
    assert "graded the head itself against its event base" in output.summary


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_grade_on_a_resolved_merge_names_no_fallback_on_its_required_check(lab: Callable[..., Run]) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates, merge_base=PARENT, graded_commit=MERGE)})
    run.engine.ports = replace(run.engine.ports, merges=Mock(return_value=(MERGE, PARENT)))
    run.cache(unit(1))
    _push(run, HEAD_A, 41)
    run.tick(1000)
    output = _required_output(run, HEAD_A)
    assert "The lab got no test merge" not in output.summary


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_grade_with_no_pull_request_number_names_no_fallback_on_its_required_check(
    lab: Callable[..., Run],
) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates)})
    run.cache(unit(1))
    run.push(HEAD_A, lane=HEAD_A, event="pull_request", action="opened")
    run.tick(1000)
    output = _required_output(run, HEAD_A)
    assert "The lab got no test merge" not in output.summary


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_notice_then_the_pull_request_event_in_one_quiet_window_grades_only_the_merge(
    lab: Callable[..., Run],
) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates, merge_base=PARENT, graded_commit=MERGE)}, quiet_ms=1000)
    run.engine.ports = replace(run.engine.ports, merges=Mock(return_value=(MERGE, PARENT)))
    notice = HeadEvent("notice", REPO, HEAD_A, BASE, ACTOR, "pushed")
    run.lab.inbox.pending.append(scheduler.Head(notice, HEAD_A))
    received = run.tick(1000)
    assert any(action.startswith(f"received {HEAD_A}") for action in received)
    _push(run, HEAD_A, 41, lane="feature")
    run.tick(1000)
    notice_key = f"{REPO}|{HEAD_A}|{int(Priority.PULL_REQUEST)}"
    assert run.lab.client.hget(scheduler.WAITING, notice_key) is None
    actions = run.tick(2000)
    assert any(action.startswith("grading ") for action in actions)
    grades = run.grades()
    assert len(grades) == 1
    assert grades[0].graded_commit == MERGE
    assert not grades[0].superseded


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_the_pull_request_event_then_a_notice_in_one_quiet_window_leaves_the_notice_unheld(
    lab: Callable[..., Run],
) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates, merge_base=PARENT, graded_commit=MERGE)}, quiet_ms=1000)
    run.engine.ports = replace(run.engine.ports, merges=Mock(return_value=(MERGE, PARENT)))
    _push(run, HEAD_A, 41, lane="feature")
    run.tick(1000)
    notice = HeadEvent("notice", REPO, HEAD_A, BASE, ACTOR, "pushed")
    run.lab.inbox.pending.append(scheduler.Head(notice, HEAD_A))
    actions = run.tick(1000)
    assert any(action.startswith(f"left {HEAD_A} to") for action in actions)
    assert len(run.lab.client.hgetall(scheduler.WAITING)) == 1
    started = run.tick(2000)
    assert any(action.startswith("grading ") for action in started)
    grades = run.grades()
    assert len(grades) == 1
    assert grades[0].graded_commit == MERGE


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_bare_notice_grade_is_superseded_once_the_pull_request_event_resolves_the_merge(
    lab: Callable[..., Run],
) -> None:
    leased_unit, pending_unit = unit(1), unit(2)
    gates = {"lint": (leased_unit, pending_unit)}
    run = lab(
        {
            HEAD_A: scheduler.Plan(TREE, BASE, gates, merge_base=PARENT, graded_commit=MERGE),
            HEAD_B: scheduler.Plan(TREE, BASE, {"lint": (unit(3),)}),
        }
    )
    run.engine.ports = replace(run.engine.ports, merges=Mock(return_value=(MERGE, PARENT)))
    notice = HeadEvent("notice", REPO, HEAD_A, BASE, ACTOR, "pushed")
    run.lab.inbox.pending.append(scheduler.Head(notice, HEAD_A))
    run.tick(1000)
    bare = run.grade(HEAD_A)
    assert bare.graded_commit == ""
    [grant] = run.lease(slots=1)
    assert grant.unit == leased_unit
    _push(run, HEAD_B, 0, lane="other")
    run.tick(1050)
    job_pending, job_other = scheduler.job_id(HEAD_A, pending_unit), scheduler.job_id(HEAD_B, unit(3))
    before = run.engine.queue.pending()
    assert before.index(job_pending) < before.index(job_other)
    _push(run, HEAD_A, 41, lane="feature")
    run.tick(1100)
    live = [grade for grade in run.grades() if grade.head == HEAD_A and not grade.superseded]
    assert len(live) == 1
    assert live[0].graded_commit == MERGE
    stale = run.engine.grade(bare.run)
    assert stale is not None and stale.superseded is True
    leased_ref, pending_ref = ref(leased_unit), ref(pending_unit)
    assert live[0].units[leased_ref].job == grant.job
    assert live[0].units[leased_ref].started is True
    assert live[0].units[leased_ref].held is False
    assert live[0].units[pending_ref].job == job_pending
    after = run.engine.queue.pending()
    assert after.index(job_pending) < after.index(job_other)
    [again] = run.lease(slots=1)
    assert again.unit == pending_unit
    run.finish(grant, "passed", {})
    run.tick(1200)
    finished = run.engine.grade(live[0].run)
    assert finished is not None and finished.units[leased_ref].outcome == "passed"


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_notice_of_a_head_already_graded_on_a_resolved_merge_is_not_held(lab: Callable[..., Run]) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates, merge_base=PARENT, graded_commit=MERGE)})
    run.engine.ports = replace(run.engine.ports, merges=Mock(return_value=(MERGE, PARENT)))
    _push(run, HEAD_A, 41, lane="feature")
    run.tick(1000)
    assert run.grade(HEAD_A).graded_commit == MERGE
    notice = HeadEvent("notice", REPO, HEAD_A, BASE, ACTOR, "pushed")
    run.lab.inbox.pending.append(scheduler.Head(notice, HEAD_A))
    actions = run.tick(1100)
    assert any(action.startswith(f"left {HEAD_A} to") for action in actions)
    grades = run.grades()
    assert len(grades) == 1
    assert not grades[0].superseded


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_notice_with_no_pull_request_waiting_is_held_and_graded_bare(lab: Callable[..., Run]) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates)})
    notice = HeadEvent("notice", REPO, HEAD_A, BASE, ACTOR, "pushed")
    run.lab.inbox.pending.append(scheduler.Head(notice, HEAD_A))
    actions = run.tick(1000)
    assert any(action.startswith("grading ") for action in actions)
    grade = run.grade(HEAD_A)
    assert grade.graded_commit == ""
    assert not grade.superseded


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_plant_head_is_held_although_a_pull_request_entry_of_the_same_head_waits(lab: Callable[..., Run]) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates)}, quiet_ms=1000)
    _push(run, HEAD_A, 41, lane="feature")
    run.tick(1000)
    _push(run, HEAD_A, 0, lane=HEAD_A, event="plant")
    actions = run.tick(1000)
    assert any(action.startswith(f"received {HEAD_A}") for action in actions)
    assert not any(action.startswith("left") for action in actions)


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_rerequest_naming_no_pull_request_is_held_although_a_pull_request_entry_of_the_same_head_waits(
    lab: Callable[..., Run],
) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates)}, quiet_ms=1000)
    _push(run, HEAD_A, 41, lane="feature")
    run.tick(1000)
    event = HeadEvent("check_run", REPO, HEAD_A, BASE, ACTOR, "rerequested")
    run.lab.inbox.pending.append(scheduler.Head(event, HEAD_A))
    actions = run.tick(1000)
    assert any(action.startswith(f"received {HEAD_A}") for action in actions)
    assert not any(action.startswith("left") for action in actions)


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_notice_carrying_a_grade_ref_is_held_although_a_pull_request_entry_of_the_same_head_waits(
    lab: Callable[..., Run],
) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates)}, quiet_ms=1000)
    _push(run, HEAD_A, 41, lane="feature")
    run.tick(1000)
    notice = HeadEvent("notice", REPO, HEAD_A, BASE, ACTOR, "pushed")
    run.lab.inbox.pending.append(scheduler.Head(notice, HEAD_A, f"{scheduler.GRADE_REFS}something"))
    actions = run.tick(1000)
    assert any(action.startswith(f"received {HEAD_A}") for action in actions)
    assert not any(action.startswith("left") for action in actions)


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_grade_on_the_same_graded_commit_is_not_replaced_by_a_second_request_resolving_it(
    lab: Callable[..., Run],
) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates, merge_base=PARENT, graded_commit=MERGE)})
    run.engine.ports = replace(run.engine.ports, merges=Mock(return_value=(MERGE, PARENT)))
    _push(run, HEAD_A, 41, lane="feature")
    run.tick(1000)
    original = run.grade(HEAD_A)
    event = HeadEvent("check_run", REPO, HEAD_A, BASE, ACTOR, "rerequested", pull_number=41)
    run.lab.inbox.pending.append(scheduler.Head(event, HEAD_A))
    run.tick(1100)
    live = [grade for grade in run.grades() if not grade.superseded]
    assert len(live) == 2
    assert original.run in {grade.run for grade in live}
    assert all(grade.graded_commit == MERGE for grade in live)


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_waiting_notice_of_another_head_is_untouched_when_a_pull_request_event_arrives(
    lab: Callable[..., Run],
) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates), HEAD_B: scheduler.Plan(TREE, BASE, gates)}, quiet_ms=1000)
    notice = HeadEvent("notice", REPO, HEAD_B, BASE, ACTOR, "pushed")
    run.lab.inbox.pending.append(scheduler.Head(notice, HEAD_B))
    run.tick(1000)
    other_key = f"{REPO}|{HEAD_B}|{int(Priority.PULL_REQUEST)}"
    assert run.lab.client.hget(scheduler.WAITING, other_key) is not None
    _push(run, HEAD_A, 41, lane="feature")
    run.tick(1000)
    assert run.lab.client.hget(scheduler.WAITING, other_key) is not None


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_live_merge_grade_in_the_promotion_class_is_neither_replaced_nor_a_reason_to_yield(
    lab: Callable[..., Run],
) -> None:
    item = unit(1)
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, {"lint": (item,)}, merge_base=PARENT, graded_commit=MERGE)})
    run.push(HEAD_A)
    run.tick(1000)
    push_grade = run.grade(HEAD_A)
    assert push_grade.tier == int(Priority.PROMOTION)
    assert not push_grade.superseded
    notice = HeadEvent("notice", REPO, HEAD_A, BASE, ACTOR, "pushed")
    run.lab.inbox.pending.append(scheduler.Head(notice, HEAD_A))
    actions = run.tick(1100)
    assert any(action.startswith("grading ") for action in actions)
    grades = run.grades()
    assert len(grades) == 2
    assert {grade.tier for grade in grades} == {int(Priority.PROMOTION), int(Priority.PULL_REQUEST)}
    stale = run.engine.grade(push_grade.run)
    assert stale is not None and not stale.superseded


OTHER_HEAD = "f" * 40


def _pr(run: Run, head: str, number: int, lane: str) -> None:
    event = HeadEvent("pull_request", REPO, head, BASE, ACTOR, "opened", lane, number)
    run.lab.inbox.pending.append(scheduler.Head(event, lane))


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_request_defaults_the_pull_number_when_the_held_entry_names_none() -> None:
    data = {
        "repo": REPO,
        "head": HEAD_A,
        "base": BASE,
        "tier": int(Priority.PULL_REQUEST),
        "actor": ACTOR,
        "bypass_negative": False,
    }
    assert scheduler._request(data).pull_number == 0


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_hold_leaves_a_waiting_entry_that_already_names_a_pull_request(lab: Callable[..., Run]) -> None:
    run = lab({}, quiet_ms=10_000)
    _pr(run, HEAD_A, 41, HEAD_A)
    run.tick(1000)
    name = f"{REPO}|{HEAD_A}|{int(Priority.PULL_REQUEST)}"
    assert json.loads(run.lab.client.hget(scheduler.WAITING, name))["request"]["pull_number"] == 41
    _pr(run, HEAD_A, 55, "other-lane")
    run.tick(1100)
    held = run.lab.client.hget(scheduler.WAITING, name)
    assert held is not None
    assert json.loads(held)["request"]["pull_number"] == 41


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_hold_defers_to_a_covering_pull_request_entry_found_past_an_unrelated_one(lab: Callable[..., Run]) -> None:
    run = lab({}, quiet_ms=10_000)
    _pr(run, OTHER_HEAD, 7, OTHER_HEAD)
    _pr(run, HEAD_B, 41, "engineer-x")
    run.tick(1000)
    notice = HeadEvent(NOTICE, REPO, HEAD_B, BASE, ACTOR, PUSHED)
    run.lab.inbox.pending.append(scheduler.Head(notice, "feature"))
    actions = run.tick(1100)
    name = f"{REPO}|engineer-x|{int(Priority.PULL_REQUEST)}"
    assert f"left {HEAD_B} to pull request 41 waiting in {name}" in actions


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_siblings_keeps_looking_past_a_live_grade_of_a_different_head(lab: Callable[..., Run]) -> None:
    plans = {
        HEAD_A: scheduler.Plan(TREE, BASE, {"lint": (unit(1),)}),
        HEAD_B: scheduler.Plan(TREE, BASE, {"lint": (unit(2),)}),
    }
    run = lab(plans)
    run.push(HEAD_B, lane="aa-other", event="pull_request", action="opened")
    run.push(HEAD_A, lane="zz-head-a", event="pull_request", action="opened")
    run.tick(1000)
    request = GradeRequest(REPO, HEAD_A, BASE, Priority.PULL_REQUEST, ACTOR, False)
    siblings = list(run.engine._siblings(request))
    assert [state.run for state in siblings] == [run.grade(HEAD_A).run]


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_siblings_skips_a_grade_ref_or_superseded_sibling_without_stopping_at_it(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, {"lint": (unit(1),)})})
    run.push(HEAD_A, lane="template")
    run.tick(2000)
    template = run.grade(HEAD_A)
    client = run.lab.client
    client.srem(scheduler.LIVE, template.run)
    client.hdel(scheduler.ORIGINS, template.run)
    client.delete(scheduler.GRADES + template.run)
    tier = int(Priority.PULL_REQUEST)
    entries = (
        (replace(template, run="bad-ref", superseded=False), "refs/grade/x"),
        (replace(template, run="good", superseded=False), ""),
        (replace(template, run="superseded", superseded=True), ""),
    )
    for state, ref_value in entries:
        run.engine.save(state)
        client.sadd(scheduler.LIVE, state.run)
        origin = {"tier": tier, "repo": REPO, "head": HEAD_A, "ref": ref_value}
        client.hset(scheduler.ORIGINS, state.run, json.dumps(origin))
    request = GradeRequest(REPO, HEAD_A, BASE, Priority.PULL_REQUEST, ACTOR, False)
    siblings = list(run.engine._siblings(request))
    assert [state.run for state in siblings] == ["good"]


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_regrade_clears_the_waiting_entry_its_own_name_identifies(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, {"lint": (unit(1),)})})
    run.push(HEAD_A)
    run.tick(1000)
    state = run.grade(HEAD_A)
    run.lab.client.hset(scheduler.WAITING, "", json.dumps({"marker": "stale"}))
    request = GradeRequest(
        state.repo, state.head, state.base, Priority(state.tier), ACTOR, False, pull_number=state.pull_number
    )
    run.engine._regrade(state, request, 2000)
    assert run.lab.client.hexists(scheduler.WAITING, "") is False


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_output_keeps_the_required_check_s_unit_count_before_the_fallback_line(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, {"lint": (unit(1),)})})
    run.engine.ports = replace(run.engine.ports, merges=Mock(return_value=None))
    _pr(run, HEAD_A, 41, HEAD_A)
    for _ in range(scheduler.UNMERGED_TRIES + 1):
        run.tick(1000)
    posted = [post for post in run.lab.poster.posts if post[0] == HEAD_A and post[1] == scheduler.REQUIRED]
    assert posted
    output = posted[-1][4]
    assert output.summary.startswith(f"1 units of {scheduler.REQUIRED} on {HEAD_A} against {BASE}")
    assert "The lab got no test merge" in output.summary
