"""A lab grade diffs against the merge base of its head and its base.

GitHub delivers a pull request's base with its head, and that base is ahead of the fork point when dev moved on
before the delivery. The static units of a grade are keyed on the files at the merge base of head and base, and each
job the grade enqueues carries that commit as the base a worker checks out, while the grader, the run key and the
base the grade names stay the delivered base.
"""

import json
import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.ci_lab import scheduler, static_exclusions
from scripts.ci_lab.delivery import CLASSES
from scripts.ci_lab.digests import GradeInputs, StaticInputs, Toolchain, grade_key, static_key
from scripts.ci_lab.intake import GradeRequest
from scripts.ci_lab.static_gates import StaticGate
from scripts.ci_lab.units import Priority, Unit
from scripts.dep_manifest import _git
from tests.ci_lab.test_scheduler import (
    ACTOR,
    BASE,
    HEAD_A,
    LOCK,
    REPO,
    TREE,
    Run,
    _blob,
    _commit,
    _gate,
    _no_tests,
    _unknown,
    unit,
)
from tests.ci_lab.test_scheduler import lab as lab
from tests.ci_lab.test_scheduler import origin as origin

FORK = "f" * 40
GRADER = "9" * 40
SIZE = _gate("size", "lint", ("src",), ("src",))


def _expander(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> scheduler.MirrorExpander:
    def discover(workflow: str, registry: object, reusable: object) -> list[StaticGate]:
        return [SIZE]

    monkeypatch.setattr(scheduler, "discover", discover)
    node = tmp_path / "node"
    _git(tmp_path, "init", "-q", "--bare", str(node))
    toolchain = Toolchain("3.12", {}, "", "", "image", {})
    return scheduler.MirrorExpander(node, tmp_path / "mirrors" / "stores", toolchain, _no_tests, _unknown)


def _moved(repo: Path, fork: str, files: dict[str, str]) -> str:
    _git(repo, "checkout", "-q", "-b", "moved", fork)
    return _commit(repo, {"src/a.py": "a = 3\n", "src/gained.py": "g = 1\n", **files})


def _size(repo: Path, head: str, base: str, grader: str) -> dict[str, tuple[Unit, ...]]:
    chain = Toolchain("3.12", {}, _blob(repo, head, LOCK), _blob(repo, head, "pyproject.toml"), "image", {})
    at_head = {"src/a.py": _blob(repo, head, "src/a.py")}
    at_base = {"src/a.py": _blob(repo, base, "src/a.py")}
    return {"lint": (Unit("static", static_key(StaticInputs("size", at_head, at_base, grader, chain)), ("size",)),)}


def _tree(repo: Path, head: str) -> str:
    return _git(repo, "rev-parse", f"{head}^{{tree}}").strip()


@pytest.mark.feature("lab-grade-diffs-against-merge-base")
def test_a_head_whose_base_moved_past_its_fork_point_is_keyed_on_the_files_of_the_merge_base(
    tmp_path: Path, origin: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, fork, head = origin
    delivered = _moved(repo, fork, {})
    request = GradeRequest(f"file://{repo}", head, delivered, Priority.PULL_REQUEST, ACTOR, False)
    found = _expander(tmp_path, monkeypatch).expand(request)
    framed = scheduler.frame(Priority.PULL_REQUEST, _tree(repo, head), delivered, _size(repo, head, fork, delivered))
    assert found == replace(framed, merge_base=fork)


@pytest.mark.feature("lab-grade-diffs-against-merge-base")
def test_a_head_whose_base_is_its_ancestor_keeps_its_keys_and_names_no_merge_base(
    tmp_path: Path, origin: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, head = origin
    request = GradeRequest(f"file://{repo}", head, base, Priority.PULL_REQUEST, ACTOR, False)
    found = _expander(tmp_path, monkeypatch).expand(request)
    assert found == scheduler.frame(Priority.PULL_REQUEST, _tree(repo, head), base, _size(repo, head, base, base))
    assert found.merge_base == ""


@pytest.mark.feature("lab-grade-diffs-against-merge-base")
def test_an_exclusion_the_base_gained_after_the_fork_point_still_excludes_its_gate(
    tmp_path: Path, origin: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, fork, head = origin
    catalog = json.dumps({"size": {"needs": ["network"], "reason": "the worker has no network"}})
    delivered = _moved(repo, fork, {static_exclusions.PATH: catalog})
    request = GradeRequest(f"file://{repo}", head, delivered, Priority.PULL_REQUEST, ACTOR, False)
    found = _expander(tmp_path, monkeypatch).expand(request)
    assert found.gates["lint"] == ()


@pytest.mark.feature("lab-grade-diffs-against-merge-base")
def test_a_head_that_shares_no_history_with_its_base_fails_expansion_at_the_merge_base(
    tmp_path: Path, origin: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, _ = origin
    _git(repo, "checkout", "-q", "--orphan", "alone")
    alone = _commit(repo, {"src/a.py": "a = 9\n"})
    request = GradeRequest(f"file://{repo}", alone, base, Priority.PULL_REQUEST, ACTOR, False)
    with pytest.raises(subprocess.CalledProcessError) as refused:
        _expander(tmp_path, monkeypatch).expand(request)
    assert "merge-base" in str(refused.value.cmd)


@pytest.mark.feature("lab-grade-diffs-against-merge-base")
def test_each_job_of_a_grade_that_names_a_merge_base_carries_it_as_the_base_a_worker_checks_out(
    lab: Callable[..., Run],
) -> None:
    run = lab({HEAD_A: scheduler.Plan(TREE, GRADER, {"lint": (unit(1),)}, merge_base=FORK)})
    run.push(HEAD_A, lane=HEAD_A, event="pull_request", action="opened")
    run.tick(1000)
    [grant] = run.lease()
    assert (grant.head, grant.base, grant.grader) == (HEAD_A, FORK, GRADER)
    grade = run.grade(HEAD_A)
    assert (grade.base, grade.merge_base) == (BASE, FORK)
    assert grade.run == grade_key(GradeInputs(REPO, HEAD_A, BASE, CLASSES[Priority.PULL_REQUEST], GRADER))


@pytest.mark.feature("lab-grade-diffs-against-merge-base")
def test_each_job_of_a_grade_that_names_no_merge_base_carries_the_grades_base(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: scheduler.Plan(TREE, GRADER, {"lint": (unit(1),)})})
    run.push(HEAD_A, lane=HEAD_A, event="pull_request", action="opened")
    run.tick(1000)
    [grant] = run.lease()
    assert (grant.head, grant.base, grant.grader) == (HEAD_A, BASE, GRADER)
    assert run.grade(HEAD_A).merge_base == ""
