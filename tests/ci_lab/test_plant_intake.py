import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scripts.ci_lab import scheduler
from scripts.ci_lab.delivery import CLASSES
from scripts.ci_lab.digests import GradeInputs, Toolchain, grade_key
from scripts.ci_lab.intake import GradeRequest
from scripts.ci_lab.objects import LocalObjects
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
    Expander,
    FakeKube,
    Inbox,
    Lab,
    Poster,
    Run,
    _commit,
    _gate,
    _no_tests,
    _unknown,
    fake_redis,
    plan,
    unit,
)
from tests.ci_lab.test_scheduler import origin as origin

PLANT = "plant"
MERGE_BASE = "9" * 40


@dataclass
class Bases(Expander):
    resolved: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    def merge_base(self, repo: str, head: str) -> str:
        self.resolved.append((repo, head))
        if head not in self.plans:
            raise subprocess.CalledProcessError(1, ["git", "merge-base", head, "dev"])
        return super().merge_base(repo, head)


def started(tmp_path: Path, bases: Bases, quiet_ms: int = 0) -> Run:
    state = Lab(fake_redis(), LocalObjects(tmp_path / "objects"), Inbox(), bases)
    return Run(state, state.build(quiet_ms, Policy(), None))


def planted(run: Run, head: str, actor: str = ACTOR) -> None:
    run.lab.inbox.pending.append(scheduler.Head(HeadEvent(PLANT, REPO, head, "", actor, "pushed"), head))


def mirrored(tmp_path: Path) -> tuple[scheduler.Scheduler, Inbox]:
    node = tmp_path / "node"
    _git(tmp_path, "init", "-q", "--bare", str(node))
    toolchain = Toolchain("3.12", {}, "", "", "", {})
    expander = scheduler.MirrorExpander(node, tmp_path / "stores", toolchain, _no_tests, _unknown)
    inbox = Inbox()
    ports = scheduler.Ports(inbox, expander, lambda _repo: Poster(), frozenset[str], FakeKube())
    settings = scheduler.Settings(frozenset({ACTOR}), 0)
    return scheduler.Scheduler(fake_redis(), LocalObjects(tmp_path / "objects"), ports, settings), inbox


def planted_in(repo: Path, head: str) -> scheduler.Head:
    return scheduler.Head(HeadEvent(PLANT, f"file://{repo}", head, "", ACTOR, "pushed"), head)


@pytest.mark.feature("lab-plant-push-intake")
def test_a_plant_push_grades_in_the_pull_request_tier_on_its_dev_merge_base(tmp_path: Path) -> None:
    bases = Bases({HEAD_A: scheduler.frame(Priority.PULL_REQUEST, TREE, MERGE_BASE, {"lint": (unit(1),)})})
    run = started(tmp_path, bases)
    planted(run, HEAD_A)
    actions = run.tick(1000)
    assert bases.resolved == [(REPO, HEAD_A)]
    assert bases.seen == [GradeRequest(REPO, HEAD_A, MERGE_BASE, Priority.PULL_REQUEST, ACTOR, False, plant=True)]
    grade = run.grade(HEAD_A)
    assert grade.run == grade_key(GradeInputs(REPO, HEAD_A, MERGE_BASE, CLASSES[Priority.PULL_REQUEST], MERGE_BASE))
    assert (grade.base, grade.grader, grade.tier) == (MERGE_BASE, MERGE_BASE, Priority.PULL_REQUEST)
    assert f"received {HEAD_A} on {HEAD_A}" in actions


@pytest.mark.feature("lab-plant-push-intake")
def test_an_ordinary_push_keeps_its_base_and_the_promotion_tier_and_resolves_nothing(tmp_path: Path) -> None:
    bases = Bases({HEAD_A: plan({"lint": (unit(1),)})})
    run = started(tmp_path, bases)
    run.push(HEAD_A)
    run.tick(1000)
    assert bases.resolved == []
    assert bases.seen == [GradeRequest(REPO, HEAD_A, BASE, Priority.PROMOTION, ACTOR, False)]


@pytest.mark.feature("lab-plant-push-intake")
def test_an_untrusted_plant_push_is_refused_before_its_base_is_resolved(tmp_path: Path) -> None:
    bases = Bases({HEAD_A: plan({"lint": (unit(1),)})})
    run = started(tmp_path, bases)
    planted(run, HEAD_A, actor="stranger")
    assert run.tick(1000) == [f"untrusted stranger head {HEAD_A}"]
    assert (bases.resolved, bases.seen) == ([], [])
    assert run.lab.client.hgetall(scheduler.WAITING) == {}


@pytest.mark.feature("lab-plant-push-intake")
def test_a_plant_head_whose_dev_merge_base_fails_is_named_and_the_next_head_still_held(tmp_path: Path) -> None:
    run = started(tmp_path, Bases({HEAD_B: plan({"lint": (unit(1),)})}), quiet_ms=500)
    planted(run, HEAD_A)
    run.push(HEAD_B, lane="other")
    failure = subprocess.CalledProcessError(1, ["git", "merge-base", HEAD_A, "dev"])
    assert run.tick(1000) == [
        f"cannot resolve the dev merge base of plant head {HEAD_A}: {failure}",
        f"received {HEAD_B} on other",
    ]
    assert list(run.lab.client.hkeys(scheduler.WAITING)) == [f"{REPO}|other|{int(Priority.PROMOTION)}"]


@pytest.mark.feature("lab-plant-push-intake")
def test_both_twins_of_a_plant_grade_on_their_dev_merge_base_and_an_unrelated_head_is_named(
    tmp_path: Path, origin: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _, fork = origin
    twins: list[str] = []
    for name in ("green", "red"):
        _git(repo, "checkout", "-q", "-b", f"diffcheck/lab-plant-size-{name}", fork)
        twins.append(_commit(repo, {"src/a.py": f"a = {name!r}\n"}))
    _git(repo, "checkout", "-q", "dev")
    _commit(repo, {"docs/c.md": "newer dev\n"})
    tree = f"{fork}^{{tree}}"
    orphan = _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit-tree", tree, "-m", "orphan").strip()
    _git(repo, "update-ref", "refs/heads/diffcheck/lab-plant-orphan", orphan)
    monkeypatch.setattr(
        scheduler, "discover", lambda workflow, registry, reusable: [_gate("size", "lint", ("src",), ())]
    )
    engine, inbox = mirrored(tmp_path)
    inbox.pending.extend(planted_in(repo, head) for head in (*twins, orphan))
    actions = engine.tick(1000)
    runs = [name.removeprefix(scheduler.GRADES) for name in engine.client.keys(scheduler.GRADES + "*")]
    grades = [grade for run in runs if (grade := engine.grade(run)) is not None]
    assert {grade.head: (grade.base, grade.grader, grade.tier) for grade in grades} == {
        twin: (fork, fork, Priority.PULL_REQUEST) for twin in twins
    }
    (refused,) = [action for action in actions if action.startswith("cannot resolve")]
    assert refused.startswith(f"cannot resolve the dev merge base of plant head {orphan}: Command ")


@pytest.mark.feature("lab-plant-push-intake")
def test_a_plant_head_of_a_remote_that_lists_no_dev_branch_is_named_and_never_held(
    tmp_path: Path, origin: tuple[Path, str, str]
) -> None:
    repo, _, head = origin
    _git(repo, "branch", "-m", "dev", "trunk")
    engine, inbox = mirrored(tmp_path)
    inbox.pending.append(planted_in(repo, head))
    (refused,) = engine.tick(1000)
    assert refused.startswith(f"cannot resolve the dev merge base of plant head {head}: Command ")
    assert "'ls-remote', '--exit-code'" in refused
    assert refused.endswith("returned non-zero exit status 2.")
    assert engine.client.hgetall(scheduler.WAITING) == {}
