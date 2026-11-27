"""The lab grades the same test merge commit GitHub Actions grades for a pull request, issue 13890.

Before it expands a pull request grade, the scheduler resolves the test merge GitHub built for it and takes it
only when its second parent is the head. While GitHub has none yet, the head is held and tried again; at the
`UNMERGED_TRIES`th try the lab gives up and grades the bare head against its event base, as it always did.
"""

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts.ci_lab import merges, scheduler
from scripts.ci_lab.delivery import CLASSES
from scripts.ci_lab.digests import GradeInputs, grade_key
from scripts.ci_lab.intake import GradeRequest
from scripts.ci_lab.units import Priority
from scripts.ci_lab.webhooks import HeadEvent
from scripts.dep_manifest import _git
from tests.ci_lab.test_scheduler import ACTOR, BASE, HEAD_A, HEAD_B, REPO, TREE, Run, unit
from tests.ci_lab.test_scheduler import lab as lab
from tests.ci_lab.test_scheduler import origin as origin
from tests.ci_lab.test_scheduler_merge_base import _expander, _tree

MERGE = "9" * 40
PARENT = "8" * 40
FEATURE = "lab-grades-the-pull-request-merge"


def _push(run: Run, head: str, number: int, lane: str = "", event: str = "pull_request") -> None:
    event_obj = HeadEvent(event, REPO, head, BASE, ACTOR, "opened", lane or head, number)
    run.lab.inbox.pending.append(scheduler.Head(event_obj, lane or head))


@pytest.mark.feature(FEATURE)
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


@pytest.mark.feature(FEATURE)
def test_a_pending_merge_resolution_holds_the_head_for_a_later_pass(lab: Callable[..., Run]) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates, merge_base=PARENT, graded_commit=MERGE)})
    port = Mock(side_effect=[merges.MergePending("still resolving"), (MERGE, PARENT)])
    run.engine.ports = replace(run.engine.ports, merges=port)
    _push(run, HEAD_A, 41)
    held = run.tick(1000)
    assert held[0].startswith(f"holding {HEAD_A}: still resolving")
    assert run.grades() == []
    assert run.tick(1000)[0].startswith("grading ")
    assert run.grade(HEAD_A).graded_commit == MERGE
    assert port.call_count == 2


@pytest.mark.feature(FEATURE)
def test_a_head_with_no_merge_yet_waits_out_the_window_then_falls_back_at_the_bound(lab: Callable[..., Run]) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates)})
    port = Mock(return_value=None)
    run.engine.ports = replace(run.engine.ports, merges=port)
    _push(run, HEAD_A, 41)
    for _ in range(scheduler.UNMERGED_TRIES):
        actions = run.tick(1000)
        assert actions[0].startswith(f"holding {HEAD_A}: GitHub has resolved no merge")
        assert run.grades() == []
    actions = run.tick(1000)
    assert actions[0].startswith("grading ")
    grade = run.grade(HEAD_A)
    assert grade.graded_commit == ""
    assert grade.merge_fallback is True
    assert port.call_count == scheduler.UNMERGED_TRIES


@pytest.mark.feature(FEATURE)
def test_github_erroring_on_every_pull_request_read_also_falls_back_at_the_bound(lab: Callable[..., Run]) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates)})
    port = Mock(side_effect=merges.MergeUnanswered("GET pulls/41 answered 500"))
    run.engine.ports = replace(run.engine.ports, merges=port)
    _push(run, HEAD_A, 41)
    for _ in range(scheduler.UNMERGED_TRIES):
        run.tick(1000)
    actions = run.tick(1000)
    assert actions[0].startswith("grading ")
    assert run.grade(HEAD_A).merge_fallback is True
    assert port.call_count == scheduler.UNMERGED_TRIES


@pytest.mark.feature(FEATURE)
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


@pytest.mark.feature(FEATURE)
def test_a_plant_grade_resolves_no_merge_even_if_it_somehow_carried_a_pull_number(lab: Callable[..., Run]) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates)})
    port = Mock(return_value=(MERGE, PARENT))
    run.engine.ports = replace(run.engine.ports, merges=port)
    _push(run, HEAD_A, 41, lane=HEAD_A, event="plant")
    run.tick(1000)
    port.assert_not_called()
    assert run.grade(HEAD_A).graded_commit == ""


@pytest.mark.feature(FEATURE)
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
    assert run.engine.queue.pending() == []
    assert run.grade(HEAD_B).graded_commit == MERGE


@pytest.mark.feature(FEATURE)
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


@pytest.mark.feature(FEATURE)
def test_a_pull_request_grade_with_no_pull_number_resolves_no_merge(lab: Callable[..., Run]) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates)})
    port = Mock(return_value=(MERGE, PARENT))
    run.engine.ports = replace(run.engine.ports, merges=port)
    run.push(HEAD_A, lane=HEAD_A, event="pull_request", action="opened")
    run.tick(1000)
    port.assert_not_called()
    assert run.grade(HEAD_A).graded_commit == ""


def _synthetic_merge(repo: Path, base: str, head: str) -> str:
    """A commit whose tree is the head's own and whose parents are base then head, as GitHub builds a test merge."""
    tree = _git(repo, "rev-parse", f"{head}^{{tree}}").strip()
    return _git(repo, "commit-tree", tree, "-p", base, "-p", head, "-m", "merge").strip()


@pytest.mark.feature("lab-grade-diffs-against-merge-base")
def test_expand_reads_the_tree_and_diffs_against_the_first_parent_of_a_resolved_merge(
    tmp_path: Path, origin: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, fork, head = origin
    merge = _synthetic_merge(repo, fork, head)
    request = GradeRequest(f"file://{repo}", head, fork, Priority.PULL_REQUEST, ACTOR, False, graded_commit=merge)
    found = _expander(tmp_path, monkeypatch).expand(request)
    assert found.graded_commit == merge
    assert found.merge_base == fork
    assert found.tree == _tree(repo, head)
