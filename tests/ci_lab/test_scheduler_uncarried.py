import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts.ci_lab import scheduler
from scripts.ci_lab.checks import Output
from scripts.ci_lab.digests import Toolchain
from scripts.dep_manifest import _git
from tests.ci_lab.test_scheduler import HEAD_A, REPO, SLUG, Run, _commit, _no_tests, _unknown, plan, pull_plan, unit
from tests.ci_lab.test_scheduler import lab as _lab
from tests.ci_lab.test_scheduler import origin as _origin

lab = _lab
origin = _origin

REASON = f"no branch or pull request head ref of {REPO} carries {HEAD_A}"


def retired(title: str) -> tuple[str, str, str, str, Output, bool]:
    output = Output(title, REASON, (), "uncarried-1000", "unknown", f"{REPO}/commit/{HEAD_A}")
    return (HEAD_A, scheduler.REQUIRED, "completed", "failure", output, True)


def expanding(tmp_path: Path) -> scheduler.MirrorExpander:
    node = tmp_path / "node"
    _git(tmp_path, "init", "-q", "--bare", str(node))
    toolchain = Toolchain("3.12", {}, "", "", "image", {})
    return scheduler.MirrorExpander(node, tmp_path / "stores", toolchain, _no_tests, _unknown)


@pytest.mark.feature("lab-scheduler-uncarried-head")
def test_a_pull_request_head_no_mirror_ref_carries_leases_no_unit_and_posts_one_failure(
    lab: Callable[..., Run],
) -> None:
    run = lab({HEAD_A: pull_plan({"lint": (unit(1),)})})
    run.lab.expander.uncarried.add(HEAD_A)
    run.push(HEAD_A, event="pull_request", action="synchronize")
    assert run.tick(1000)[-1] == f"retiring {HEAD_A}: {REASON}"
    assert run.lease(slots=5) == []
    assert run.lab.poster.posts == [retired("pull_request grade retired")]
    assert run.lab.repos == [SLUG]
    assert run.lab.expander.seen == []
    assert run.lab.client.hgetall(scheduler.WAITING) == {}
    assert run.grades() == []


@pytest.mark.feature("lab-scheduler-uncarried-head")
def test_a_hotfix_head_no_mirror_ref_carries_posts_one_failure_in_its_class(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: pull_plan({"lint": (unit(1),)})})
    run.lab.fixes.add("fix-branch")
    run.lab.expander.uncarried.add(HEAD_A)
    run.push(HEAD_A, lane="fix-branch", event="pull_request", action="synchronize")
    assert run.tick(1000)[-1] == f"retiring {HEAD_A}: {REASON}"
    assert run.lease(slots=5) == []
    assert run.lab.poster.posts == [retired("hotfix grade retired")]


@pytest.mark.feature("lab-scheduler-uncarried-head")
def test_a_merge_group_head_no_mirror_ref_carries_leases_no_unit_and_posts_nothing(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: plan({"unit": (unit(1, "test"),)})})
    run.lab.expander.uncarried.add(HEAD_A)
    run.push(HEAD_A, lane="queue", event="merge_group", action="checks_requested")
    assert run.tick(1000)[-1] == f"retiring {HEAD_A}: {REASON}"
    assert run.lease(slots=5) == []
    assert run.lab.poster.posts == []
    assert run.grades() == []


@pytest.mark.feature("lab-scheduler-uncarried-head")
def test_a_head_a_mirror_ref_carries_is_asked_once_and_grades_its_units(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: pull_plan({"lint": (unit(1),)})})
    run.push(HEAD_A, event="pull_request", action="synchronize")
    assert run.tick(1000)[-1].startswith("grading ")
    assert run.lab.expander.checked == [HEAD_A]
    assert len(run.lease(slots=5)) == 1


@pytest.mark.feature("lab-scheduler-uncarried-head")
def test_a_grade_ref_head_grades_without_asking_whether_a_mirror_ref_carries_it(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)})})
    run.lab.expander.uncarried.add(HEAD_A)
    run.push(HEAD_A, lane="engineer-1", ref="refs/grade/t1")
    assert run.tick(1000)[-1].startswith("grading ")
    assert run.lab.expander.checked == []
    assert run.grade(HEAD_A).ref == "refs/grade/t1"


@pytest.mark.feature("lab-scheduler-uncarried-head")
def test_the_mirror_expander_carries_only_heads_a_branch_a_pull_request_head_or_dev_reaches(
    tmp_path: Path, origin: tuple[Path, str, str]
) -> None:
    repo, base, head = origin
    _git(repo, "checkout", "-q", "-b", "gh-readonly-queue/dev/pr-1-live", head)
    queued = _commit(repo, {"src/a.py": "a = 3\n"})
    _git(repo, "checkout", "-q", "-b", "gh-readonly-queue/dev/pr-2-ejected", head)
    ejected = _commit(repo, {"src/a.py": "a = 4\n"})
    _git(repo, "checkout", "-q", "dev")
    _git(repo, "branch", "-q", "-D", "gh-readonly-queue/dev/pr-2-ejected")
    made: dict[str, str] = {}
    for name in ("head", "merge"):
        tree = f"{head}^{{tree}}"
        sha = _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit-tree", tree, "-p", head, "-m", name)
        made[name] = sha.strip()
        _git(repo, "update-ref", f"refs/pull/7/{name}", made[name])
    expander = expanding(tmp_path)
    heads = {"dev": head, "dev ancestor": base, "queue": queued, "pull": made["head"]}
    misses = {"ejected": ejected, "pull merge": made["merge"]}
    found = {name: expander.carried(f"file://{repo}", sha) for name, sha in {**heads, **misses}.items()}
    assert found == {**dict.fromkeys(heads, True), **dict.fromkeys(misses, False)}


@pytest.mark.feature("lab-scheduler-uncarried-head")
def test_a_remote_the_mirror_expander_cannot_list_fails_the_check_instead_of_retiring_the_head(
    tmp_path: Path, origin: tuple[Path, str, str]
) -> None:
    _, _, head = origin
    missing = f"file://{tmp_path / 'missing'}"
    with pytest.raises(subprocess.CalledProcessError) as raised:
        expanding(tmp_path).carried(missing, head)
    assert raised.value.cmd == ["git", "ls-remote", missing, "refs/heads/*", "refs/pull/*/head"]
