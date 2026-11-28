"""Tests for the lab's test merge resolution: one pull request read and a git parent check, off the scheduler
pass, issue 13890.
"""

import contextlib
import subprocess
import threading
from collections.abc import Mapping
from concurrent.futures import Future, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest
import requests

from scripts import github_app
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
def test_resolve_wraps_a_failed_fetch_of_the_merge_as_unanswered(tmp_path: Path) -> None:
    repo, base, head = _repo(tmp_path)
    merge = _merge_commit(repo, base, head)
    reader = FakePullRequests({41: {"merge_commit_sha": merge}})
    mirror = Mirror(tmp_path / "node", tmp_path / "store", str(tmp_path / "no-such-remote"))
    with pytest.raises(merges.MergeUnanswered, match="fetching the merge"):
        merges.resolve(reader, mirror, 41, head)


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_resolve_wraps_a_timed_out_fetch_of_the_merge_as_unanswered(tmp_path: Path) -> None:
    repo, base, head = _repo(tmp_path)
    merge = _merge_commit(repo, base, head)
    reader = FakePullRequests({41: {"merge_commit_sha": merge}})
    mirror = _mirror(tmp_path, repo)
    with pytest.raises(merges.MergeUnanswered, match="fetching the merge"):
        merges.resolve(reader, mirror, 41, head, timeout=1e-9)


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_resolve_wraps_a_timed_out_parents_read_as_unanswered(tmp_path: Path) -> None:
    repo, base, head = _repo(tmp_path)
    merge = _merge_commit(repo, base, head)
    reader = FakePullRequests({41: {"merge_commit_sha": merge}})
    mirror = _mirror(tmp_path, repo)
    mirror.fetch(merge)
    with pytest.raises(merges.MergeUnanswered, match="fetching the merge"):
        merges.resolve(reader, mirror, 41, head, timeout=1e-9)


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


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_the_resolver_builds_its_reader_for_the_owner_and_name_of_a_repository_address() -> None:
    made: list[str] = []
    asked: list[str] = []

    def reader(slug: str) -> FakePullRequests:
        made.append(slug)
        return FakePullRequests({41: {}})

    def mirror(repo: str) -> Mirror:
        asked.append(repo)
        return Mirror(Path("node"), Path("store"), repo)

    resolver = merges.Resolver(mirror, reader)
    assert resolver("https://github.com/example/repo", 41, HEAD) is None
    assert made == ["example/repo"]
    assert asked == ["https://github.com/example/repo"]


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


def _finished(fetches: Mapping[tuple[str, int, str], tuple[list[float], Future[tuple[str, str] | None]]]) -> None:
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
def test_background_spends_no_try_on_a_resolution_still_waiting_for_a_thread() -> None:
    first, second = ("example/repo", 41, HEAD), ("example/repo", 42, OTHER)
    held = Held({first: (MERGE, BASE), second: (MERGE, BASE)})
    clock = Clock()
    background = merges.Background(held, workers=1, clock=clock, bound_s=5.0)
    with pytest.raises(merges.MergePending):
        background(*first)
    assert held.started.acquire(timeout=WAIT_S)
    with pytest.raises(merges.MergePending):
        background(*second)
    clock.now = 5.0
    with pytest.raises(merges.MergePending):
        background(*second)
    with pytest.raises(merges.MergeUnanswered, match="did not resolve within 5s"):
        background(*first)
    assert held.asked == [first]
    held.release.set()
    assert held.started.acquire(timeout=WAIT_S)
    _finished(background.resolutions)
    assert background(*second) == (MERGE, BASE)
    assert held.asked == [first, second]


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_background_bounds_a_queued_resolution_from_when_a_thread_began_it() -> None:
    first, second = ("example/repo", 41, HEAD), ("example/repo", 42, OTHER)
    gates = {first: threading.Event(), second: threading.Event()}
    begun = threading.Semaphore(0)

    def resolve(repo: str, number: int, head: str) -> tuple[str, str] | None:
        begun.release()
        gates[(repo, number, head)].wait(WAIT_S)
        return MERGE, BASE

    clock = Clock()
    background = merges.Background(resolve, workers=1, clock=clock, bound_s=5.0)
    with pytest.raises(merges.MergePending):
        background(*first)
    assert begun.acquire(timeout=WAIT_S)
    with pytest.raises(merges.MergePending):
        background(*second)
    clock.now = 4.0
    gates[first].set()
    assert begun.acquire(timeout=WAIT_S)
    clock.now = 8.0
    with pytest.raises(merges.MergePending):
        background(*second)
    clock.now = 9.0
    with pytest.raises(merges.MergeUnanswered, match="did not resolve within 5s"):
        background(*second)
    gates[second].set()


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_background_answers_a_found_merge_until_its_fresh_window_ends_then_reads_github_again() -> None:
    key = ("example/repo", 41, HEAD)
    held = Held({key: (MERGE, BASE)})
    clock = Clock(now=10.0)
    background = merges.Background(held, clock=clock, fresh_s=30.0)
    with pytest.raises(merges.MergePending):
        background(*key)
    assert held.started.acquire(timeout=WAIT_S)
    held.release.set()
    _finished(background.resolutions)
    held.release.clear()
    clock.now = 39.0
    assert background(*key) == (MERGE, BASE)
    assert background(*key) == (MERGE, BASE)
    assert held.asked == [key]
    clock.now = 40.0
    assert background(*key) == (MERGE, BASE)
    with pytest.raises(merges.MergePending):
        background(*key)
    assert held.started.acquire(timeout=WAIT_S)
    assert held.asked == [key, key]
    held.release.set()


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_background_forgets_a_merge_its_head_read_once_its_fresh_window_ended_and_another_head_is_asked() -> None:
    found, other = ("example/repo", 41, HEAD), ("example/repo", 44, OTHER)
    held = Held({found: (MERGE, BASE), other: (MERGE, BASE)})
    clock = Clock()
    background = merges.Background(held, clock=clock, fresh_s=30.0)
    with pytest.raises(merges.MergePending):
        background(*found)
    assert held.started.acquire(timeout=WAIT_S)
    held.release.set()
    _finished(background.resolutions)
    assert background(*found) == (MERGE, BASE)
    clock.now = 29.0
    with contextlib.suppress(merges.MergePending):
        background(*other)
    assert set(background.resolutions) == {found, other}
    clock.now = 30.0
    with contextlib.suppress(merges.MergePending):
        background(*other)
    assert set(background.resolutions) == {other}


@pytest.mark.feature("lab-grades-the-pull-request-merge")
@pytest.mark.parametrize("kept_s", [None, 120.0], ids=["an-hour-by-default", "the-window-given"])
def test_background_keeps_an_answer_no_head_read_until_its_kept_window_ends_while_another_head_is_asked(
    kept_s: float | None,
) -> None:
    found, failed, absent = (("example/repo", number, HEAD) for number in (41, 42, 43))
    other = ("example/repo", 44, OTHER)
    held = Held({found: (MERGE, BASE), failed: RuntimeError("boom"), absent: None, other: (MERGE, BASE)})
    clock = Clock(now=100.0)
    if kept_s is None:
        background, window = merges.Background(held, clock=clock), 3600.0
    else:
        background, window = merges.Background(held, clock=clock, kept_s=kept_s), kept_s
    for key in (found, failed, absent):
        with pytest.raises(merges.MergePending):
            background(*key)
        assert held.started.acquire(timeout=WAIT_S)
    held.release.set()
    _finished(background.resolutions)
    clock.now = 100.0 + window - 1.0
    with contextlib.suppress(merges.MergePending):
        background(*other)
    assert set(background.resolutions) == {found, failed, absent, other}
    clock.now = 100.0 + window
    with contextlib.suppress(merges.MergePending):
        background(*other)
    assert set(background.resolutions) == {other}


@pytest.mark.feature("lab-grades-the-pull-request-merge")
@pytest.mark.parametrize("waited", [0.0, 31.0], ids=["inside-the-fresh-window", "past-the-fresh-window"])
def test_background_returns_a_failed_resolution_to_its_own_head_after_another_head_was_asked(waited: float) -> None:
    failed, other = ("example/repo", 41, HEAD), ("example/repo", 44, OTHER)
    held = Held({failed: merges.MergeRefused("GET pulls/41 answered 404"), other: (MERGE, BASE)})
    clock = Clock()
    background = merges.Background(held, clock=clock, fresh_s=30.0)
    with pytest.raises(merges.MergePending):
        background(*failed)
    assert held.started.acquire(timeout=WAIT_S)
    held.release.set()
    _finished(background.resolutions)
    clock.now = waited
    with contextlib.suppress(merges.MergePending):
        background(*other)
    with pytest.raises(merges.MergeRefused, match="answered 404"):
        background(*failed)
    assert held.asked.count(failed) == 1


@pytest.mark.feature("lab-grades-the-pull-request-merge")
@pytest.mark.parametrize("waited", [0.0, 31.0], ids=["inside-the-fresh-window", "past-the-fresh-window"])
def test_background_returns_an_absent_answer_to_its_own_head_after_another_head_was_asked(waited: float) -> None:
    absent, other = ("example/repo", 43, HEAD), ("example/repo", 44, OTHER)
    held = Held({absent: None, other: (MERGE, BASE)})
    clock = Clock()
    background = merges.Background(held, clock=clock, fresh_s=30.0)
    with pytest.raises(merges.MergePending):
        background(*absent)
    assert held.started.acquire(timeout=WAIT_S)
    held.release.set()
    _finished(background.resolutions)
    clock.now = waited
    with contextlib.suppress(merges.MergePending):
        background(*other)
    assert background(*absent) is None
    assert held.asked.count(absent) == 1


@pytest.mark.feature("lab-grades-the-pull-request-merge")
@pytest.mark.parametrize("waited", [0.0, 31.0], ids=["inside-the-fresh-window", "past-the-fresh-window"])
def test_background_returns_a_found_merge_to_its_own_head_after_another_head_was_asked(waited: float) -> None:
    found, other = ("example/repo", 41, HEAD), ("example/repo", 44, OTHER)
    held = Held({found: (MERGE, BASE), other: (MERGE, BASE)})
    clock = Clock()
    background = merges.Background(held, clock=clock, fresh_s=30.0)
    with pytest.raises(merges.MergePending):
        background(*found)
    assert held.started.acquire(timeout=WAIT_S)
    held.release.set()
    _finished(background.resolutions)
    clock.now = waited
    with contextlib.suppress(merges.MergePending):
        background(*other)
    assert background(*found) == (MERGE, BASE)
    assert held.asked.count(found) == 1


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_background_keeps_the_answer_of_a_head_resolved_again_after_its_read_merge_left() -> None:
    key, other = ("example/repo", 41, HEAD), ("example/repo", 44, OTHER)
    held = Held({key: (MERGE, BASE), other: (MERGE, BASE)})
    clock = Clock()
    background = merges.Background(held, clock=clock, fresh_s=30.0)
    with pytest.raises(merges.MergePending):
        background(*key)
    assert held.started.acquire(timeout=WAIT_S)
    held.release.set()
    _finished(background.resolutions)
    assert background(*key) == (MERGE, BASE)
    clock.now = 30.0
    assert background(*key) == (MERGE, BASE)
    held.release.clear()
    with pytest.raises(merges.MergePending):
        background(*key)
    assert held.started.acquire(timeout=WAIT_S)
    held.release.set()
    _finished(background.resolutions)
    clock.now = 61.0
    with contextlib.suppress(merges.MergePending):
        background(*other)
    assert background(*key) == (MERGE, BASE)
    assert held.asked.count(key) == 2


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_background_gives_each_waiting_head_its_own_failure_when_a_pass_asks_them_in_turn() -> None:
    keys = [("example/repo", number, HEAD) for number in (41, 42, 43)]
    held = Held({key: merges.MergeRefused(f"GET pulls/{key[1]} answered 404") for key in keys})
    background = merges.Background(held, clock=Clock())
    for key in keys:
        with pytest.raises(merges.MergePending):
            background(*key)
        assert held.started.acquire(timeout=WAIT_S)
    held.release.set()
    _finished(background.resolutions)
    for key in keys:
        with pytest.raises(merges.MergeRefused, match=f"^GET pulls/{key[1]} answered 404$"):
            background(*key)
    assert held.asked == keys


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


class Token:
    """A token source that always hands out the fixture token."""

    def get_token(self) -> str:
        return "fixture"


class RefusingToken:
    """A token source whose mint GitHub refuses, with or without a status code."""

    def __init__(self, status_code: int | None) -> None:
        self.status_code = status_code

    def get_token(self) -> str:
        raise github_app.GitAuthRefused("denied", status_code=self.status_code)


def _response(status: int, body: dict[str, Any] | None = None) -> Mock:
    return Mock(status_code=status, json=Mock(return_value=dict(body or {})))


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_actions_sends_the_repo_path_auth_and_timeout_and_returns_the_parsed_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        merges, "GitHubAppAuth", Mock(side_effect=AssertionError("must not mint a token when one is given"))
    )
    get = Mock(return_value=_response(200, {"number": 41}))
    monkeypatch.setattr(requests, "get", get)
    actions = merges.Actions("owner/name", auth=Token())
    assert actions.json("pulls/41") == {"number": 41}
    get.assert_called_once_with(
        f"{merges.API}/repos/owner/name/pulls/41",
        headers={"Authorization": "Bearer fixture", **merges.HEADERS},
        timeout=merges.TIMEOUT,
    )


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_actions_json_raises_merge_refused_for_a_client_error_with_the_exact_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(requests, "get", Mock(return_value=_response(404)))
    actions = merges.Actions("owner/name", auth=Token())
    with pytest.raises(merges.MergeRefused, match="^GET pulls/41 answered 404$") as excinfo:
        actions.json("pulls/41")
    assert type(excinfo.value) is merges.MergeRefused


@pytest.mark.feature("lab-grades-the-pull-request-merge")
@pytest.mark.parametrize("status", [500, 503])
def test_actions_json_raises_merge_unanswered_at_and_above_the_server_error_boundary(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setattr(requests, "get", Mock(return_value=_response(status)))
    actions = merges.Actions("owner/name", auth=Token())
    with pytest.raises(merges.MergeUnanswered, match=f"^GET pulls/41 answered {status}$") as excinfo:
        actions.json("pulls/41")
    assert type(excinfo.value) is merges.MergeUnanswered


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_actions_get_wraps_a_transport_error_as_unanswered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(requests, "get", Mock(side_effect=requests.ConnectionError("boom")))
    actions = merges.Actions("owner/name", auth=Token())
    with pytest.raises(merges.MergeUnanswered, match="^GET pulls/41 failed: boom$"):
        actions.json("pulls/41")


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_actions_get_raises_merge_refused_when_the_token_mint_is_refused_without_a_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        requests, "get", Mock(side_effect=AssertionError("must not call GitHub when the token mint fails"))
    )
    actions = merges.Actions("owner/name", auth=RefusingToken(None))
    with pytest.raises(merges.MergeRefused, match="^GET pulls/41 token mint failed: denied$") as excinfo:
        actions.json("pulls/41")
    assert type(excinfo.value) is merges.MergeRefused


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_actions_get_raises_merge_unanswered_when_the_token_mint_is_refused_with_a_server_error_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        requests, "get", Mock(side_effect=AssertionError("must not call GitHub when the token mint fails"))
    )
    actions = merges.Actions("owner/name", auth=RefusingToken(500))
    with pytest.raises(merges.MergeUnanswered, match="^GET pulls/41 token mint failed: denied$") as excinfo:
        actions.json("pulls/41")
    assert type(excinfo.value) is merges.MergeUnanswered


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_actions_keeps_the_given_auth_and_builds_no_github_app_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    build = Mock(side_effect=AssertionError("must not build a GitHubAppAuth when an auth is given"))
    monkeypatch.setattr(merges, "GitHubAppAuth", build)
    token = Token()
    actions = merges.Actions("owner/name", auth=token)
    assert actions.auth is token


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_actions_builds_a_scoped_github_app_auth_when_none_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    build = Mock(return_value=Token())
    monkeypatch.setattr(merges, "GitHubAppAuth", build)
    actions = merges.Actions("owner/name")
    assert actions.auth is build.return_value
    build.assert_called_once_with("github.com/owner/name", permissions=merges.PERMISSIONS)


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_the_resolvers_default_reader_reads_a_repository_address_by_its_owner_and_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build = Mock(return_value=Token())
    monkeypatch.setattr(merges, "GitHubAppAuth", build)
    get = Mock(return_value=_response(200, {"number": 41}))
    monkeypatch.setattr(requests, "get", get)

    def mirror(repo: str) -> Mirror:
        return Mirror(Path("node"), Path("store"), repo)

    resolver = merges.Resolver(mirror)
    assert resolver("https://github.com/owner/name", 41, HEAD) is None
    build.assert_called_once_with("github.com/owner/name", permissions=merges.PERMISSIONS)
    get.assert_called_once_with(
        f"{merges.API}/repos/owner/name/pulls/41",
        headers={"Authorization": "Bearer fixture", **merges.HEADERS},
        timeout=merges.TIMEOUT,
    )


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_x_parents_raises_when_git_reports_a_bad_revision(tmp_path: Path) -> None:
    repo, _base, head = _repo(tmp_path)
    mirror = _mirror(tmp_path, repo)
    mirror.fetch(head)
    with pytest.raises(subprocess.CalledProcessError):
        merges._parents(mirror, "0" * 40, timeout=5)


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_resolve_passes_its_own_timeout_to_the_mirror_fetch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, base, head = _repo(tmp_path)
    merge = _merge_commit(repo, base, head)
    reader = FakePullRequests({41: {"merge_commit_sha": merge}})
    mirror = _mirror(tmp_path, repo)
    original = Mirror.fetch
    calls: list[tuple[str, float | None]] = []

    def fetch(self: Mirror, sha: str, timeout: float | None = None) -> bool:
        calls.append((sha, timeout))
        return original(self, sha, timeout=timeout)

    monkeypatch.setattr(Mirror, "fetch", fetch)
    assert merges.resolve(reader, mirror, 41, head, timeout=17.5) == (merge, base)
    assert calls == [(merge, 17.5)]


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_the_resolver_calls_resolve_with_the_reader_mirror_number_head_and_git_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mirrors: dict[str, Mirror] = {}

    def mirror(slug: str) -> Mirror:
        return mirrors.setdefault(slug, Mirror(Path("node"), Path("store"), slug))

    def reader(slug: str) -> FakePullRequests:
        return FakePullRequests({41: {}})

    resolve = Mock(return_value=(MERGE, BASE))
    monkeypatch.setattr(merges, "resolve", resolve)
    resolver = merges.Resolver(mirror, reader)
    assert resolver("example/repo", 41, HEAD) == (MERGE, BASE)
    resolve.assert_called_once_with(
        resolver.readers["example/repo"], mirrors["example/repo"], 41, HEAD, timeout=merges.GIT_TIMEOUT
    )


class _StillRunning:
    """A resolution future that must not be read while it is still marked as running."""

    def done(self) -> bool:
        return False

    def exception(self) -> BaseException | None:
        raise AssertionError("exception() must not be read while a resolution is still running")

    def result(self, timeout: float | None = None) -> tuple[str, str] | None:
        raise AssertionError("result() must not be read while a resolution is still running")


@pytest.mark.feature("lab-grades-the-pull-request-merge")
@pytest.mark.parametrize("read", [True, False], ids=["read", "unread"])
def test_fresh_treats_a_still_running_resolution_as_fresh_without_reading_its_result(read: bool) -> None:
    background = merges.Background(lambda *_: None, clock=lambda: 0.0)
    future = cast("Future[tuple[str, str] | None]", _StillRunning())
    assert background._fresh(0.0, ([], future), read=read) is True


@pytest.mark.feature("lab-grades-the-pull-request-merge")
@pytest.mark.parametrize("answer", [(MERGE, BASE), None, RuntimeError("boom")], ids=["found", "absent", "failed"])
def test_fresh_measures_the_window_from_when_the_resolution_began_not_added_to_the_clock(
    answer: tuple[str, str] | Exception | None,
) -> None:
    background = merges.Background(lambda *_: None, clock=lambda: 0.0, fresh_s=30.0, kept_s=120.0)
    future: Future[tuple[str, str] | None] = Future()
    if isinstance(answer, Exception):
        future.set_exception(answer)
    else:
        future.set_result(answer)
    assert background._fresh(129.0, ([100.0], future), read=True) is True
    assert background._fresh(130.0, ([100.0], future), read=True) is False
    assert background._fresh(219.0, ([100.0], future), read=False) is True
    assert background._fresh(220.0, ([100.0], future), read=False) is False
