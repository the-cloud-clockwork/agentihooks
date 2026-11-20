import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scripts.ci_lab import scheduler
from scripts.ci_lab.checks import Output
from scripts.ci_lab.digests import Toolchain
from scripts.ci_lab.objects import LocalObjects
from scripts.ci_lab.webhooks import HeadEvent
from scripts.dep_manifest import _git
from tests.ci_lab.test_scheduler import (
    ACTOR,
    HEAD_A,
    HEAD_B,
    REPO,
    SLUG,
    FakeKube,
    Inbox,
    Poster,
    Run,
    _commit,
    _no_tests,
    _unknown,
    fake_redis,
    plan,
    pull_plan,
    unit,
)
from tests.ci_lab.test_scheduler import lab as _lab
from tests.ci_lab.test_scheduler import origin as _origin
from tests.ci_lab.test_scheduler_posts import REFUSALS, refusing

lab = _lab
origin = _origin

REASON = f"no branch or pull request head ref of {REPO} carries {HEAD_A}"


@dataclass
class Reach:
    refused: frozenset[str]
    asked: list[str] = field(default_factory=list[str])

    def __call__(self, repo: str, head: str) -> bool:
        self.asked.append(head)
        return head not in self.refused


def reaching(run: Run, monkeypatch: pytest.MonkeyPatch, *refused: str) -> Reach:
    reach = Reach(frozenset(refused))
    monkeypatch.setattr(run.lab.expander, "reachable", reach)
    return reach


def retired(repo: str, head: str) -> tuple[str, str, str, str, Output, bool]:
    reason = f"no branch or pull request head ref of {repo} carries {head}"
    output = Output("pull_request grade retired", reason, (), "uncarried-1000", "unknown", f"{repo}/commit/{head}")
    return (head, scheduler.REQUIRED, "completed", "failure", output, True)


def expanding(tmp_path: Path) -> scheduler.MirrorExpander:
    node = tmp_path / "node"
    _git(tmp_path, "init", "-q", "--bare", str(node))
    toolchain = Toolchain("3.12", {}, "", "", "image", {})
    return scheduler.MirrorExpander(node, tmp_path / "stores", toolchain, _no_tests, _unknown)


def ejected(repo: Path, head: str) -> str:
    branch = "gh-readonly-queue/dev/pr-2-ejected"
    _git(repo, "checkout", "-q", "-b", branch, head)
    sha = _commit(repo, {"src/a.py": "a = 4\n"})
    _git(repo, "checkout", "-q", "dev")
    _git(repo, "branch", "-q", "-D", branch)
    _git(repo, "reflog", "expire", "--expire=now", "--all")
    _git(repo, "gc", "-q", "--prune=now")
    return sha


@pytest.mark.feature("lab-scheduler-uncarried-head")
def test_a_pull_request_head_no_mirror_ref_reaches_leases_no_unit_and_posts_one_failure(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = lab({HEAD_A: pull_plan({"lint": (unit(1),)})})
    reaching(run, monkeypatch, HEAD_A)
    run.push(HEAD_A, event="pull_request", action="synchronize")
    assert run.tick(1000)[-1] == f"retiring {HEAD_A}: {REASON}"
    assert run.lease(slots=5) == []
    assert run.lab.poster.posts == [retired(REPO, HEAD_A)]
    assert run.lab.repos == [SLUG]
    assert run.lab.expander.seen == []
    assert run.lab.client.hgetall(scheduler.WAITING) == {}
    assert run.grades() == []


@pytest.mark.feature("lab-scheduler-uncarried-head")
@pytest.mark.parametrize("refusal", REFUSALS)
def test_a_refused_failure_post_of_a_retired_pull_request_head_is_recorded_and_the_pass_goes_on(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch, refusal: Exception
) -> None:
    run = lab({HEAD_A: pull_plan({"lint": (unit(1),)}), HEAD_B: plan({"lint": (unit(2),)})})
    reaching(run, monkeypatch, HEAD_A)
    poster = refusing(run, refusal, heads=frozenset({HEAD_A}))
    run.push(HEAD_A, lane="first", event="pull_request", action="synchronize")
    run.push(HEAD_B, lane="second")
    actions = run.tick(1000)
    retiring = f"retiring {HEAD_A}: {REASON}"
    assert f"{retiring}; cannot post the failed {scheduler.REQUIRED} check on {HEAD_A}: {refusal}" in actions
    assert poster.moves(HEAD_A, scheduler.REQUIRED) == []
    assert poster.moves(HEAD_B, "lint") == [("queued", None)]


@pytest.mark.feature("lab-scheduler-uncarried-head")
def test_a_hotfix_head_no_mirror_ref_reaches_is_retired_without_a_post(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = lab({HEAD_A: pull_plan({"lint": (unit(1),)})})
    run.lab.fixes.add("fix-branch")
    reaching(run, monkeypatch, HEAD_A)
    run.push(HEAD_A, lane="fix-branch", event="pull_request", action="synchronize")
    assert run.tick(1000)[-1] == f"retiring {HEAD_A}: {REASON}"
    assert run.lease(slots=5) == []
    assert run.lab.poster.posts == []


@pytest.mark.feature("lab-scheduler-uncarried-head")
@pytest.mark.parametrize(("lane", "event"), [("queue", "merge_group"), ("dev", "push"), ("dev", "backfill")])
def test_a_queue_promotion_or_backfill_head_no_mirror_ref_reaches_is_retired_without_a_post(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch, lane: str, event: str
) -> None:
    run = lab({HEAD_A: plan({"unit": (unit(1, "test"),)})})
    reaching(run, monkeypatch, HEAD_A)
    run.push(HEAD_A, lane=lane, event=event)
    assert run.tick(1000)[-1] == f"retiring {HEAD_A}: {REASON}"
    assert run.lease(slots=5) == []
    assert run.lab.poster.posts == []
    assert run.grades() == []


@pytest.mark.feature("lab-scheduler-uncarried-head")
def test_a_head_a_mirror_ref_reaches_is_asked_once_and_grades_its_units(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = lab({HEAD_A: pull_plan({"lint": (unit(1),)})})
    reach = reaching(run, monkeypatch)
    run.push(HEAD_A, event="pull_request", action="synchronize")
    assert any(action.startswith("grading ") for action in run.tick(1000))
    assert reach.asked == [HEAD_A]
    assert len(run.lease(slots=5)) == 1


@pytest.mark.feature("lab-scheduler-uncarried-head")
def test_a_grade_ref_head_grades_without_asking_whether_a_mirror_ref_reaches_it(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)})})
    reach = reaching(run, monkeypatch, HEAD_A)
    run.push(HEAD_A, lane="engineer-1", ref="refs/grade/t1")
    assert any(action.startswith("grading ") for action in run.tick(1000))
    assert reach.asked == []
    assert run.grade(HEAD_A).ref == "refs/grade/t1"


@pytest.mark.feature("lab-scheduler-uncarried-head")
def test_the_mirror_expander_reaches_only_heads_a_branch_a_pull_request_head_or_dev_holds(
    tmp_path: Path, origin: tuple[Path, str, str]
) -> None:
    repo, base, head = origin
    _git(repo, "checkout", "-q", "-b", "gh-readonly-queue/dev/pr-1-live", head)
    queued = _commit(repo, {"src/a.py": "a = 3\n"})
    _git(repo, "checkout", "-q", "dev")
    gone = ejected(repo, head)
    made: dict[str, str] = {}
    for name in ("head", "merge"):
        tree = f"{head}^{{tree}}"
        sha = _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit-tree", tree, "-p", head, "-m", name)
        made[name] = sha.strip()
        _git(repo, "update-ref", f"refs/pull/7/{name}", made[name])
    expander = expanding(tmp_path)
    heads = {"dev": head, "dev ancestor": base, "queue": queued, "pull": made["head"]}
    misses = {"ejected": gone, "pull merge": made["merge"]}
    found = {name: expander.reachable(f"file://{repo}", sha) for name, sha in {**heads, **misses}.items()}
    assert found == {**dict.fromkeys(heads, True), **dict.fromkeys(misses, False)}


@pytest.mark.feature("lab-scheduler-uncarried-head")
def test_a_remote_the_mirror_expander_cannot_list_raises_the_listing_error(
    tmp_path: Path, origin: tuple[Path, str, str]
) -> None:
    _, _, head = origin
    missing = f"file://{tmp_path / 'missing'}"
    with pytest.raises(subprocess.CalledProcessError) as raised:
        expanding(tmp_path).reachable(missing, head)
    assert raised.value.cmd == ["git", "ls-remote", missing, "refs/heads/*", "refs/pull/*/head"]


@pytest.mark.feature("lab-scheduler-uncarried-head")
def test_a_pull_request_head_gone_from_every_ref_and_the_remote_is_retired_through_the_mirror_expander(
    tmp_path: Path, origin: tuple[Path, str, str]
) -> None:
    repo, _, head = origin
    gone = ejected(repo, head)
    inbox, poster = Inbox(), Poster()
    ports = scheduler.Ports(inbox, expanding(tmp_path), lambda _repo: poster, frozenset[str], FakeKube())
    settings = scheduler.Settings(frozenset({ACTOR}), 0)
    engine = scheduler.Scheduler(fake_redis(), LocalObjects(tmp_path / "objects"), ports, settings)
    remote = f"file://{repo}"
    inbox.pending.append(scheduler.Head(HeadEvent("pull_request", remote, gone, head, ACTOR, "synchronize"), "pr-2"))
    reason = f"no branch or pull request head ref of {remote} carries {gone}"
    assert f"retiring {gone}: {reason}" in engine.tick(1000)
    assert poster.posts == [retired(remote, gone)]
