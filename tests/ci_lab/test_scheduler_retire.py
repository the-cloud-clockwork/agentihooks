import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scripts.ci_lab import evidence, scheduler
from scripts.ci_lab.digests import Toolchain
from scripts.ci_lab.units import Priority
from scripts.dep_manifest import _git
from tests.ci_lab.test_scheduler import (
    BASE,
    HEAD_A,
    HEAD_B,
    OTHER_TREE,
    REPO,
    TREE,
    Run,
    _no_tests,
    _unknown,
    pull_plan,
    unit,
)
from tests.ci_lab.test_scheduler import lab as lab
from tests.ci_lab.test_scheduler import origin as origin


def _records(run: Run) -> dict[str, dict[str, Any]]:
    return {name: json.loads(raw) for name, raw in run.lab.client.hgetall(scheduler.KINDS).items()}


@pytest.mark.feature("lab-retire-gone-merge-groups")
def test_a_merge_group_grade_whose_ref_is_gone_is_retired_and_posts_nothing(lab: Callable[..., Run]) -> None:
    first, second = unit(1), unit(2)
    run = lab({HEAD_A: scheduler.frame(Priority.QUEUE, TREE, BASE, {"unit": (first, second)})})
    run.push(HEAD_A, lane=HEAD_A, event="merge_group", action="checks_requested")
    run.tick(1000)
    live = run.grade(HEAD_A).run
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_A, item) for item in (first, second)]
    posts = len(run.lab.poster.posts)
    run.lab.expander.gone.add(HEAD_A)
    assert run.tick(1100) == [f"retired {live}: no merge group ref of {REPO} carries {HEAD_A}, dropped 2 pending units"]
    assert run.lab.client.smembers(scheduler.LIVE) == set()
    assert run.lab.client.get(scheduler.GRADES + live) is None
    assert run.engine.queue.pending() == []
    assert len(run.lab.poster.posts) == posts
    assert list(run.lab.objects.keys(evidence.head_prefix(HEAD_A))) == []
    assert run.lab.client.get(f"{scheduler.TREES}{REPO}|{TREE}") is None
    assert not run.engine.registry.values.get(scheduler.contract.HEADS_GRADED_TOTAL)
    assert _records(run) == {}


@pytest.mark.feature("lab-retire-gone-merge-groups")
def test_a_merge_group_grade_whose_ref_is_listed_keeps_its_units_and_its_verdict(lab: Callable[..., Run]) -> None:
    lost, kept = unit(1), unit(2)
    run = lab(
        {
            HEAD_A: scheduler.frame(Priority.QUEUE, TREE, BASE, {"unit": (lost,)}),
            HEAD_B: scheduler.frame(Priority.QUEUE, OTHER_TREE, BASE, {"unit": (kept,)}),
        }
    )
    run.push(HEAD_A, lane=HEAD_A, event="merge_group", action="checks_requested")
    run.push(HEAD_B, lane=HEAD_B, event="merge_group", action="checks_requested")
    run.tick(1000)
    run.lab.expander.gone.add(HEAD_A)
    run.tick(1100)
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_B, kept)]
    run.finish(run.lease()[0], "passed", {"seconds": 2.0})
    run.tick(1200)
    assert [grade.head for grade in run.grades()] == [HEAD_B]
    graded = run.grade(HEAD_B)
    assert graded.conclusion == "success"
    assert run.lab.client.get(f"{scheduler.TREES}{REPO}|{OTHER_TREE}") == graded.run


@pytest.mark.feature("lab-retire-gone-merge-groups")
def test_a_merge_group_listing_git_cannot_produce_retires_nothing(lab: Callable[..., Run]) -> None:
    item = unit(1)
    run = lab({HEAD_A: scheduler.frame(Priority.QUEUE, TREE, BASE, {"unit": (item,)})})
    run.push(HEAD_A, lane=HEAD_A, event="merge_group", action="checks_requested")
    run.tick(1000)
    run.lab.expander.gone.add(HEAD_A)
    run.lab.expander.unlisted = True
    actions = run.tick(1100)
    assert [action.partition(": ")[0] for action in actions] == [f"cannot list the merge groups of {REPO}"]
    assert run.lab.client.smembers(scheduler.LIVE) == {run.grade(HEAD_A).run}
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_A, item)]


@pytest.mark.feature("lab-retire-gone-merge-groups")
def test_a_retired_merge_group_grade_leaves_the_jobs_another_live_grade_of_its_head_holds(
    lab: Callable[..., Run],
) -> None:
    item = unit(1)
    run = lab({HEAD_A: scheduler.frame(Priority.QUEUE, TREE, BASE, {"unit": (item,)})})
    run.push(HEAD_A, lane=HEAD_A, event="merge_group", action="checks_requested")
    run.push(HEAD_A, lane="dev")
    run.tick(1000)
    queued, pushed = sorted(run.grades(), key=lambda grade: grade.tier)
    assert (queued.tier, pushed.tier) == (Priority.QUEUE, Priority.PROMOTION)
    run.lab.expander.gone.add(HEAD_A)
    actions = run.tick(1100)
    assert actions[0] == f"retired {queued.run}: no merge group ref of {REPO} carries {HEAD_A}, dropped 0 pending units"
    assert run.lab.client.smembers(scheduler.LIVE) == {pushed.run}
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_A, item)]


@pytest.mark.feature("lab-retire-gone-merge-groups")
def test_a_merge_group_grade_pushed_to_a_grade_ref_is_never_retired(lab: Callable[..., Run]) -> None:
    item = unit(1)
    run = lab({HEAD_A: scheduler.frame(Priority.QUEUE, TREE, BASE, {"unit": (item,)})})
    ref = f"{scheduler.GRADE_REFS}feature"
    run.push(HEAD_A, lane=HEAD_A, event="merge_group", action="checks_requested", ref=ref)
    run.tick(1000)
    run.lab.expander.gone.add(HEAD_A)
    run.tick(1100)
    assert run.lab.client.smembers(scheduler.LIVE) == {run.grade(HEAD_A).run}
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_A, item)]


@pytest.mark.feature("lab-retire-gone-merge-groups")
def test_a_live_grade_without_a_record_is_recorded_and_the_record_of_a_closed_run_is_removed(
    lab: Callable[..., Run],
) -> None:
    item = unit(1)
    run = lab({HEAD_A: scheduler.frame(Priority.QUEUE, TREE, BASE, {"unit": (item,)})})
    run.push(HEAD_A, lane=HEAD_A, event="merge_group", action="checks_requested")
    run.tick(1000)
    live = run.grade(HEAD_A).run
    record = {"tier": int(Priority.QUEUE), "repo": REPO, "head": HEAD_A, "ref": ""}
    assert _records(run) == {live: record}
    run.lab.client.delete(scheduler.KINDS)
    run.lab.client.hset(scheduler.KINDS, "closed", json.dumps(record))
    assert run.tick(1100) == []
    assert _records(run) == {live: record}
    run.lab.expander.gone.add(HEAD_A)
    run.lab.client.delete(scheduler.KINDS)
    assert run.tick(1200) == [f"retired {live}: no merge group ref of {REPO} carries {HEAD_A}, dropped 1 pending units"]
    assert run.lab.client.smembers(scheduler.LIVE) == set()
    assert _records(run) == {}


@pytest.mark.feature("lab-retire-gone-merge-groups")
def test_a_pull_request_grade_is_never_retired_for_a_missing_merge_group_ref(lab: Callable[..., Run]) -> None:
    item = unit(1)
    run = lab({HEAD_A: pull_plan({"lint": (item,)})})
    run.push(HEAD_A, event="pull_request", action="opened")
    run.tick(1000)
    run.lab.expander.gone.add(HEAD_A)
    run.tick(1100)
    assert run.lab.client.smembers(scheduler.LIVE) == {run.grade(HEAD_A).run}
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_A, item)]


@pytest.mark.feature("lab-retire-gone-merge-groups")
def test_the_mirror_expander_lists_the_head_of_every_merge_group_ref(
    tmp_path: Path, origin: tuple[Path, str, str]
) -> None:
    repo, base, head = origin
    _git(repo, "branch", f"gh-readonly-queue/dev/pr-1-{base}", head)
    _git(repo, "branch", "feature", base)
    node = tmp_path / "node"
    _git(tmp_path, "init", "-q", "--bare", str(node))
    toolchain = Toolchain("3.12", {}, "", "", "image", {})
    expander = scheduler.MirrorExpander(node, tmp_path / "stores", toolchain, _no_tests, _unknown)
    assert expander.groups(f"file://{repo}") == frozenset({head})
    _git(repo, "branch", "-D", f"gh-readonly-queue/dev/pr-1-{base}")
    assert expander.groups(f"file://{repo}") == frozenset()
