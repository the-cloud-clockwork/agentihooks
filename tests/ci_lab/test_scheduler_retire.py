import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from scripts.ci_lab import evidence, scheduler
from scripts.ci_lab.digests import Toolchain
from scripts.ci_lab.units import Priority, Unit
from scripts.ci_lab.webhooks import HeadEvent
from scripts.dep_manifest import _git
from tests.ci_lab.test_scheduler import (
    ACTOR,
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

OTHER_REPO = f"{REPO}-bundle"


def _origins(run: Run) -> dict[str, dict[str, Any]]:
    return {name: json.loads(raw) for name, raw in run.lab.client.hgetall(scheduler.ORIGINS).items()}


def _queued(lab: Callable[..., Run], *units: Unit) -> Run:
    run = lab({HEAD_A: scheduler.frame(Priority.QUEUE, TREE, BASE, {"unit": units})})
    run.push(HEAD_A, lane=HEAD_A, event="merge_group", action="checks_requested")
    run.tick(1000)
    return run


def _elsewhere(run: Run, head: str, lane: str, event: str = "merge_group", action: str = "checks_requested") -> None:
    run.lab.inbox.pending.append(scheduler.Head(HeadEvent(event, OTHER_REPO, head, BASE, ACTOR, action), lane, ""))


def _retired(run: str, dropped: int, repo: str = REPO, head: str = HEAD_A) -> str:
    return f"retired {run}: no merge group ref of {repo} carries {head}, dropped {dropped} pending units"


def _mirror(tmp_path: Path) -> scheduler.MirrorExpander:
    node = tmp_path / "node"
    _git(tmp_path, "init", "-q", "--bare", str(node))
    toolchain = Toolchain("3.12", {}, "", "", "image", {})
    return scheduler.MirrorExpander(node, tmp_path / "stores", toolchain, _no_tests, _unknown)


@pytest.mark.feature("lab-retire-gone-merge-groups")
def test_a_merge_group_grade_whose_ref_is_gone_leaves_the_live_set_and_loses_its_pending_jobs(
    lab: Callable[..., Run],
) -> None:
    first, second = unit(1), unit(2)
    run = _queued(lab, first, second)
    live = run.grade(HEAD_A).run
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_A, item) for item in (first, second)]
    run.lab.expander.gone.add(HEAD_A)
    assert run.tick(1100) == [_retired(live, 2)]
    assert run.lab.client.smembers(scheduler.LIVE) == set()
    assert run.lab.client.get(scheduler.GRADES + live) is None
    assert _origins(run) == {}
    assert run.engine.queue.pending() == []


@pytest.mark.feature("lab-retire-gone-merge-groups")
def test_a_retired_merge_group_grade_posts_nothing_and_records_no_verdict(lab: Callable[..., Run]) -> None:
    run = _queued(lab, unit(1))
    posts = len(run.lab.poster.posts)
    run.lab.expander.gone.add(HEAD_A)
    run.tick(1100)
    assert len(run.lab.poster.posts) == posts
    assert list(run.lab.objects.keys(evidence.head_prefix(HEAD_A))) == []
    assert run.lab.client.get(f"{scheduler.TREES}{REPO}|{TREE}") is None
    assert not run.engine.registry.values.get(scheduler.contract.HEADS_GRADED_TOTAL)


@pytest.mark.feature("lab-retire-gone-merge-groups")
def test_a_retired_merge_group_grade_leaves_no_revision_and_no_demand_record(lab: Callable[..., Run]) -> None:
    run = _queued(lab, unit(1))
    live = run.grade(HEAD_A).run
    assert run.lab.client.hkeys(scheduler.REVISIONS) == [live]
    assert run.lab.client.hkeys(scheduler.RECORDS) == [live]
    run.lab.expander.gone.add(HEAD_A)
    run.tick(1100)
    assert run.lab.client.hkeys(scheduler.REVISIONS) == []
    assert run.lab.client.hkeys(scheduler.RECORDS) == []


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
@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(128, ["git", "ls-remote", REPO]),
        subprocess.TimeoutExpired(["git", "ls-remote", REPO], scheduler.GROUPS_TIMEOUT_S),
    ],
)
def test_a_merge_group_listing_git_cannot_produce_retires_nothing(
    lab: Callable[..., Run], error: subprocess.SubprocessError
) -> None:
    item = unit(1)
    run = _queued(lab, item)
    run.lab.expander.gone.add(HEAD_A)
    run.lab.expander.unlisted[REPO] = error
    assert run.tick(1100) == [f"cannot list the merge groups of {REPO}: {error}"]
    assert run.lab.client.smembers(scheduler.LIVE) == {run.grade(HEAD_A).run}
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_A, item)]


@pytest.mark.feature("lab-retire-gone-merge-groups")
def test_a_listing_git_cannot_produce_for_one_repository_still_retires_the_gone_grades_of_another(
    lab: Callable[..., Run],
) -> None:
    run = lab(
        {
            HEAD_A: scheduler.frame(Priority.QUEUE, TREE, BASE, {"unit": (unit(1),)}),
            HEAD_B: scheduler.frame(Priority.QUEUE, OTHER_TREE, BASE, {"unit": (unit(2),)}),
        }
    )
    run.push(HEAD_A, lane=HEAD_A, event="merge_group", action="checks_requested")
    _elsewhere(run, HEAD_B, lane=HEAD_B)
    run.tick(1000)
    unlisted, listed = run.grade(HEAD_A).run, run.grade(HEAD_B).run
    error = subprocess.CalledProcessError(128, ["git", "ls-remote", REPO])
    run.lab.expander.unlisted[REPO] = error
    run.lab.expander.gone.update({HEAD_A, HEAD_B})
    assert run.tick(1100) == [
        f"cannot list the merge groups of {REPO}: {error}",
        _retired(listed, 1, OTHER_REPO, HEAD_B),
    ]
    assert run.lab.client.smembers(scheduler.LIVE) == {unlisted}


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
    assert run.tick(1100)[0] == _retired(queued.run, 0)
    assert run.lab.client.smembers(scheduler.LIVE) == {pushed.run}
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_A, item)]


@pytest.mark.feature("lab-retire-gone-merge-groups")
def test_a_retired_merge_group_grade_leaves_the_jobs_a_live_grade_of_its_head_in_another_repository_holds(
    lab: Callable[..., Run],
) -> None:
    item = unit(1)
    run = lab({HEAD_A: scheduler.frame(Priority.QUEUE, TREE, BASE, {"unit": (item,)})})
    run.push(HEAD_A, lane=HEAD_A, event="merge_group", action="checks_requested")
    _elsewhere(run, HEAD_A, lane="dev", event="push", action="pushed")
    run.tick(1000)
    queued, pushed = sorted(run.grades(), key=lambda grade: grade.tier)
    assert (queued.repo, pushed.repo) == (REPO, OTHER_REPO)
    run.lab.expander.gone.add(HEAD_A)
    assert run.tick(1100)[0] == _retired(queued.run, 0)
    assert run.lab.client.smembers(scheduler.LIVE) == {pushed.run}
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_A, item)]


@pytest.mark.feature("lab-retire-gone-merge-groups")
def test_a_retired_merge_group_grade_leaves_its_leased_jobs_running(lab: Callable[..., Run]) -> None:
    leased, waiting = unit(1), unit(2)
    run = _queued(lab, leased, waiting)
    live = run.grade(HEAD_A).run
    [grant] = run.lease()
    run.lab.expander.gone.add(HEAD_A)
    assert run.tick(1100) == [_retired(live, 1)]
    assert run.engine.queue.pending() == []
    assert run.engine.queue.leased() == [grant.job]


@pytest.mark.feature("lab-retire-gone-merge-groups")
def test_an_expired_lease_of_a_retired_merge_group_grade_is_requeued_once_and_then_lost(
    lab: Callable[..., Run],
) -> None:
    run = _queued(lab, unit(1))
    live = run.grade(HEAD_A).run
    [first] = run.lease(ttl_ms=100)
    run.lab.expander.gone.add(HEAD_A)
    assert run.tick(1050) == [_retired(live, 0)]
    assert run.tick(1100) == [f"reaped {first.job} pending"]
    [second] = run.lease(ttl_ms=100)
    assert run.tick(1300) == [f"reaped {second.job} lost"]
    assert run.engine.queue.pending() == []
    assert run.engine.queue.leased() == []


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
def test_a_grade_records_its_origin_when_it_starts(lab: Callable[..., Run]) -> None:
    run = _queued(lab, unit(1))
    origin = {"tier": int(Priority.QUEUE), "repo": REPO, "head": HEAD_A, "ref": ""}
    assert _origins(run) == {run.grade(HEAD_A).run: origin}


@pytest.mark.feature("lab-retire-gone-merge-groups")
def test_a_live_grade_with_no_origin_gets_one_from_its_grade_document(lab: Callable[..., Run]) -> None:
    run = _queued(lab, unit(1))
    recorded = _origins(run)
    run.lab.client.delete(scheduler.ORIGINS)
    assert run.tick(1100) == []
    assert _origins(run) == recorded


@pytest.mark.feature("lab-retire-gone-merge-groups")
def test_a_grade_document_with_no_ref_gives_an_origin_with_an_empty_ref(lab: Callable[..., Run]) -> None:
    run = _queued(lab, unit(1))
    live = run.grade(HEAD_A).run
    recorded = _origins(run)
    document = json.loads(run.lab.client.get(scheduler.GRADES + live))
    del document["ref"]
    run.lab.client.set(scheduler.GRADES + live, json.dumps(document))
    run.lab.client.delete(scheduler.ORIGINS)
    run.tick(1100)
    assert _origins(run) == recorded


@pytest.mark.feature("lab-retire-gone-merge-groups")
def test_a_live_run_with_no_grade_document_holds_no_other_origin_back(lab: Callable[..., Run]) -> None:
    run = _queued(lab, unit(1))
    recorded = _origins(run)
    run.lab.client.delete(scheduler.ORIGINS)
    run.lab.client.sadd(scheduler.LIVE, "0")
    run.tick(1100)
    assert _origins(run) == recorded


@pytest.mark.feature("lab-retire-gone-merge-groups")
def test_a_steady_pass_reads_no_grade_document_for_the_origins(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _queued(lab, unit(1))
    recorded = _origins(run)
    read = Mock(wraps=run.lab.client.get)
    monkeypatch.setattr(run.lab.client, "get", read)
    assert run.engine._origins() == recorded
    read.assert_not_called()


@pytest.mark.feature("lab-retire-gone-merge-groups")
def test_a_live_grade_with_no_origin_is_retired_in_the_pass_that_records_it(lab: Callable[..., Run]) -> None:
    run = _queued(lab, unit(1))
    live = run.grade(HEAD_A).run
    run.lab.client.delete(scheduler.ORIGINS)
    run.lab.expander.gone.add(HEAD_A)
    assert run.tick(1100) == [_retired(live, 1)]
    assert run.lab.client.smembers(scheduler.LIVE) == set()
    assert _origins(run) == {}


@pytest.mark.feature("lab-retire-gone-merge-groups")
def test_the_origin_of_a_run_that_is_no_longer_live_is_removed(lab: Callable[..., Run]) -> None:
    run = _queued(lab, unit(1))
    recorded = _origins(run)
    run.lab.client.hset(scheduler.ORIGINS, "closed", json.dumps(next(iter(recorded.values()))))
    run.tick(1100)
    assert _origins(run) == recorded


@pytest.mark.feature("lab-retire-gone-merge-groups")
def test_the_mirror_expander_lists_the_head_of_every_merge_group_ref(
    tmp_path: Path, origin: tuple[Path, str, str]
) -> None:
    repo, base, head = origin
    _git(repo, "branch", f"gh-readonly-queue/dev/pr-1-{base}", head)
    _git(repo, "branch", "feature", base)
    expander = _mirror(tmp_path)
    assert expander.groups(f"file://{repo}") == frozenset({head})
    _git(repo, "branch", "-D", f"gh-readonly-queue/dev/pr-1-{base}")
    assert expander.groups(f"file://{repo}") == frozenset()


@pytest.mark.feature("lab-retire-gone-merge-groups")
def test_the_mirror_expander_fails_for_a_repository_git_cannot_list(tmp_path: Path) -> None:
    expander = _mirror(tmp_path)
    with pytest.raises(subprocess.CalledProcessError):
        expander.groups(f"file://{tmp_path / 'missing'}")


@pytest.mark.feature("lab-retire-gone-merge-groups")
def test_the_mirror_expander_bounds_the_merge_group_listing_by_its_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expander = _mirror(tmp_path)
    listed = subprocess.CompletedProcess[str]([], 0, stdout=f"{HEAD_A}\trefs/heads/gh-readonly-queue/dev/pr-1\n")
    listing = Mock(return_value=listed)
    monkeypatch.setattr(subprocess, "run", listing)
    assert expander.groups(REPO) == frozenset({HEAD_A})
    assert listing.call_args.kwargs["timeout"] == scheduler.GROUPS_TIMEOUT_S
