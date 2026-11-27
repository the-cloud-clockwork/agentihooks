"""The lab grades the same test merge commit GitHub Actions grades for a pull request, issue 13890.

Before it expands a pull request grade, the scheduler resolves the test merge GitHub built for it and takes it
only when its second parent is the head. While GitHub has none yet, the head is held and tried again; at the
`UNMERGED_TRIES`th try the lab gives up and grades the bare head against its event base, as it always did.
"""

import threading
from collections.abc import Callable
from concurrent.futures import wait as wait_futures
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from scripts.ci_lab import budget, merges, scheduler
from scripts.ci_lab.checks import Output
from scripts.ci_lab.delivery import CLASSES
from scripts.ci_lab.digests import GradeInputs, grade_key
from scripts.ci_lab.intake import GradeRequest
from scripts.ci_lab.objects import LocalObjects
from scripts.ci_lab.outcomes import DROPPED
from scripts.ci_lab.retry import Policy
from scripts.ci_lab.units import Priority
from scripts.ci_lab.webhooks import HeadEvent
from scripts.dep_manifest import _git
from tests.ci_lab.test_scheduler import (
    ACTOR,
    BASE,
    HEAD_A,
    HEAD_B,
    REPO,
    TREE,
    Consumer,
    FakeChecks,
    Run,
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
    assert held[-1].startswith(f"holding {HEAD_A}: still resolving")
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
    for _ in range(scheduler.UNMERGED_TRIES):
        actions = run.tick(1000)
        assert actions[-1].startswith(f"holding {HEAD_A}: GitHub has resolved no merge")
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
    leased_unit, held_unit = unit(1), unit(2)
    gates = {"lint": (leased_unit, held_unit)}
    other_merge, other_parent = "7" * 40, "6" * 40
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates, merge_base=PARENT, graded_commit=MERGE)})
    port = Mock(side_effect=[(MERGE, PARENT), (other_merge, other_parent)])
    run.engine.ports = replace(run.engine.ports, merges=port)
    _push(run, HEAD_A, 41)
    run.tick(1000)
    old = run.grade(HEAD_A)
    assert old.graded_commit == MERGE
    [grant] = run.lease(slots=1)
    assert grant.unit == leased_unit
    event = HeadEvent("check_run", REPO, HEAD_A, BASE, ACTOR, "rerequested", pull_number=41)
    run.lab.inbox.pending.append(scheduler.Head(event, HEAD_A))
    run.tick(2000)
    live = [grade for grade in run.grades() if not grade.superseded]
    assert [grade.head for grade in live] == [HEAD_A]
    assert live[0].graded_commit == other_merge
    assert live[0].run != old.run
    assert live[0].units[ref(leased_unit)].started is True
    assert live[0].units[ref(leased_unit)].held is False
    assert live[0].units[ref(leased_unit)].outcome == ""
    stale = run.engine.grade(old.run)
    assert stale is not None and stale.units[ref(held_unit)].outcome == DROPPED
    assert any(post[0] == HEAD_A for post in run.lab.poster.posts)
    assert port.call_count == 2


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
