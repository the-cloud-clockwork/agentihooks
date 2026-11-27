"""Tests for the lab's test merge resolution: one pull request read and a git parent check, off the scheduler
pass, issue 13890.
"""

import threading
from collections.abc import Mapping
from concurrent.futures import Future, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from scripts.ci_lab import merges
from scripts.ci_lab.worktrees import Mirror
from scripts.dep_manifest import _git

BASE = "b" * 40
HEAD = "h" * 40
OTHER = "o" * 40
MERGE = "m" * 40


def _repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "-q", str(repo))
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "a.txt").write_text("b\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "head")
    head = _git(repo, "rev-parse", "HEAD").strip()
    return repo, base, head


def _merge_commit(repo: Path, first: str, second: str) -> str:
    tree = _git(repo, "rev-parse", f"{second}^{{tree}}").strip()
    return _git(repo, "commit-tree", tree, "-p", first, "-p", second, "-m", "merge").strip()


def _mirror(tmp_path: Path, remote: Path) -> Mirror:
    return Mirror(tmp_path / "node", tmp_path / "store", str(remote))


@dataclass
class FakePullRequests:
    pulls: dict[int, dict[str, Any]]
    asked: list[str] = field(default_factory=list[str])

    def json(self, path: str) -> dict[str, Any]:
        self.asked.append(path)
        number = int(path.removeprefix("pulls/"))
        return self.pulls[number]


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_resolve_returns_the_merge_and_its_first_parent_when_its_second_parent_is_the_head(tmp_path: Path) -> None:
    repo, base, head = _repo(tmp_path)
    merge = _merge_commit(repo, base, head)
    reader = FakePullRequests({41: {"merge_commit_sha": merge}})
    mirror = _mirror(tmp_path, repo)
    assert merges.resolve(reader, mirror, 41, head) == (merge, base)
    assert reader.asked == ["pulls/41"]


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_resolve_answers_none_when_the_merges_second_parent_is_not_the_head(tmp_path: Path) -> None:
    repo, base, head = _repo(tmp_path)
    merge = _merge_commit(repo, base, head)
    reader = FakePullRequests({41: {"merge_commit_sha": merge}})
    mirror = _mirror(tmp_path, repo)
    assert merges.resolve(reader, mirror, 41, OTHER) is None


@pytest.mark.feature("lab-grades-the-pull-request-merge")
@pytest.mark.parametrize("pull", [{}, {"merge_commit_sha": None}, {"merge_commit_sha": ""}])
def test_resolve_answers_none_for_a_pull_request_with_no_merge_commit(tmp_path: Path, pull: dict[str, Any]) -> None:
    reader = FakePullRequests({41: pull})
    mirror = _mirror(tmp_path, tmp_path / "unused")
    assert merges.resolve(reader, mirror, 41, HEAD) is None


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_resolve_lets_a_refused_pull_request_read_propagate() -> None:
    class Refusing:
        def json(self, path: str) -> dict[str, Any]:
            raise merges.MergeUnanswered(f"GET {path} answered 500")

    with pytest.raises(merges.MergeUnanswered, match="answered 500"):
        merges.resolve(Refusing(), Mirror(Path("node"), Path("store"), "unused"), 41, HEAD)


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_the_resolver_reads_each_repository_through_one_reader_of_its_own() -> None:
    made: list[str] = []

    def reader(slug: str) -> FakePullRequests:
        made.append(slug)
        return FakePullRequests({41: {}})

    mirrors: dict[str, Mirror] = {}

    def mirror(slug: str) -> Mirror:
        return mirrors.setdefault(slug, Mirror(Path("node"), Path("store"), slug))

    resolver = merges.Resolver(mirror, reader)
    assert resolver("example/repo", 41, HEAD) is None
    assert resolver("example/repo", 41, HEAD) is None
    assert resolver("other/repo", 41, HEAD) is None
    assert made == ["example/repo", "other/repo"]
    assert merges.Resolver(mirror).reader is merges.Actions


WAIT_S = 10.0


@dataclass
class Clock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now


@dataclass
class Held:
    answers: dict[tuple[str, int, str], tuple[str, str] | None | Exception]
    release: threading.Event = field(default_factory=threading.Event)
    started: threading.Semaphore = field(default_factory=lambda: threading.Semaphore(0))
    asked: list[tuple[str, int, str]] = field(default_factory=list[tuple[str, int, str]])

    def __call__(self, repo: str, number: int, head: str) -> tuple[str, str] | None:
        self.asked.append((repo, number, head))
        self.started.release()
        self.release.wait(WAIT_S)
        answer = self.answers[(repo, number, head)]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _finished(fetches: Mapping[tuple[str, int, str], tuple[float, Future[tuple[str, str] | None]]]) -> None:
    wait([future for _, future in fetches.values()])


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_background_raises_pending_while_a_resolution_runs_and_answers_it_once_finished(tmp_path: Path) -> None:
    held = Held({("example/repo", 41, HEAD): (MERGE, BASE)})
    clock = Clock()
    background = merges.Background(held, clock=clock)
    with pytest.raises(merges.MergePending, match=f"^the merge of example/repo#41 for {HEAD} is still resolving$"):
        background("example/repo", 41, HEAD)
    held.release.set()
    _finished(background.resolutions)
    assert background("example/repo", 41, HEAD) == (MERGE, BASE)
    assert held.asked == [("example/repo", 41, HEAD)]


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_background_raises_the_resolutions_own_error_once_on_a_later_ask() -> None:
    held = Held({("example/repo", 41, HEAD): merges.MergeUnanswered("GET pulls/41 answered 500")})
    clock = Clock()
    background = merges.Background(held, clock=clock)
    with pytest.raises(merges.MergePending):
        background("example/repo", 41, HEAD)
    assert held.started.acquire(timeout=WAIT_S)
    held.release.set()
    _finished(background.resolutions)
    with pytest.raises(merges.MergeUnanswered, match="answered 500"):
        background("example/repo", 41, HEAD)
    held.release.clear()
    with pytest.raises(merges.MergePending):
        background("example/repo", 41, HEAD)
    assert held.started.acquire(timeout=WAIT_S)
    assert held.asked == [("example/repo", 41, HEAD), ("example/repo", 41, HEAD)]
    held.release.set()


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_background_raises_unanswered_once_a_resolution_runs_past_its_bound() -> None:
    held = Held({("example/repo", 41, HEAD): (MERGE, BASE)})
    clock = Clock()
    background = merges.Background(held, clock=clock, bound_s=5.0)
    with pytest.raises(merges.MergePending):
        background("example/repo", 41, HEAD)
    assert held.started.acquire(timeout=WAIT_S)
    clock.now = 5.0
    with pytest.raises(merges.MergeUnanswered, match="did not resolve within 5s"):
        background("example/repo", 41, HEAD)
    held.release.set()


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_background_reads_github_again_after_an_absent_answer() -> None:
    held = Held({("example/repo", 41, HEAD): None})
    clock = Clock()
    background = merges.Background(held, clock=clock)
    with pytest.raises(merges.MergePending):
        background("example/repo", 41, HEAD)
    assert held.started.acquire(timeout=WAIT_S)
    held.release.set()
    _finished(background.resolutions)
    assert background("example/repo", 41, HEAD) is None
    held.release.clear()
    with pytest.raises(merges.MergePending):
        background("example/repo", 41, HEAD)
    assert held.started.acquire(timeout=WAIT_S)
    assert held.asked == [("example/repo", 41, HEAD), ("example/repo", 41, HEAD)]
    held.release.set()


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_background_wraps_any_other_error_of_the_resolver_as_unanswered() -> None:
    held = Held({("example/repo", 41, HEAD): RuntimeError("boom")})
    clock = Clock()
    background = merges.Background(held, clock=clock)
    with pytest.raises(merges.MergePending):
        background("example/repo", 41, HEAD)
    assert held.started.acquire(timeout=WAIT_S)
    held.release.set()
    _finished(background.resolutions)
    with pytest.raises(merges.MergeUnanswered, match="boom"):
        background("example/repo", 41, HEAD)


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_background_passes_a_merge_refused_through_without_wrapping_it() -> None:
    held = Held({("example/repo", 41, HEAD): merges.MergeRefused("GET pulls/41 answered 404")})
    clock = Clock()
    background = merges.Background(held, clock=clock)
    with pytest.raises(merges.MergePending):
        background("example/repo", 41, HEAD)
    assert held.started.acquire(timeout=WAIT_S)
    held.release.set()
    _finished(background.resolutions)
    with pytest.raises(merges.MergeRefused, match="answered 404"):
        background("example/repo", 41, HEAD)
