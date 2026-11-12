"""Swarm pr records only a pull request GitHub confirms for the task's branch, and intent waits for its body and diff."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, NoReturn

import pytest

from scripts.gates import intent
from scripts.swarm import cli
from scripts.swarm.store import SwarmError
from scripts.tick.adapters.fakes import FakeSourceControl
from scripts.tick.ports.source_control import ChangeRef, ChangeState
from tests.swarm.test_cli import env, run  # noqa: F401

URL = "https://github.com/o/r/pull/3"
MINE = "engineer-a1b2c3-0001"


def _started(env: Any, monkeypatch: pytest.MonkeyPatch, pull: cli.PullHead) -> dict[str, Any]:  # noqa: F811
    _, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    ledger.rows["t1"]["branch"] = MINE
    monkeypatch.setattr(cli, "pull_branch", lambda url: pull)
    return ledger.rows["t1"]


def _ports(state: str, branch: str = MINE) -> Callable[[str], tuple[FakeSourceControl, ChangeRef]]:
    reference = ChangeRef("o/r", 3)
    change = ChangeState(reference, state, "abc123", "dev", False, "u", branch, "", False, "")
    return lambda url: (FakeSourceControl({"change": change}), reference)


def test_swarm_pr_refuses_a_pull_request_on_another_branch(
    env: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    row = _started(env, monkeypatch, cli.PullHead("someone-else", "", 3, "abc123"))
    capsys.readouterr()
    assert run("sw", "--as", "engineer@a1b2c3-0001", "pr", URL) != 0
    refusal = f"pull request {URL} is on branch someone-else, not this task's recorded branch {MINE}"
    assert capsys.readouterr().err.strip().endswith(refusal)
    assert not row.get("pr_url") and row["state"] != "pr"


def test_swarm_pr_stores_the_confirmed_number_and_head(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    row = _started(env, monkeypatch, cli.PullHead(MINE, "", 3, "abc123"))
    assert run("sw", "--as", "engineer@a1b2c3-0001", "pr", URL) == 0
    assert (row["pr_url"], row["branch"], row["pr_number"], row["pr_head"]) == (URL, MINE, 3, "abc123")


def test_the_pull_branch_returns_the_number_and_head_of_an_open_pull_request() -> None:
    assert cli.pull_branch(URL, _ports("open")) == cli.PullHead(MINE, "", 3, "abc123")


def test_the_pull_branch_refuses_a_closed_pull_request() -> None:
    with pytest.raises(SwarmError) as refused:
        cli.pull_branch(URL, _ports("closed"))
    assert str(refused.value) == f"pull request {URL} is closed, not open"


def test_the_pull_branch_refuses_a_pull_request_github_does_not_confirm() -> None:
    def ports(url: str) -> NoReturn:
        raise KeyError("no such pull request")

    with pytest.raises(SwarmError) as refused:
        cli.pull_branch(URL, ports)
    assert str(refused.value) == f"GitHub does not confirm pull request {URL}: 'no such pull request'"


@pytest.mark.parametrize(
    ("body", "files", "missing"),
    [("", ["a.py"], "a body"), ("Closes 1", [], "a diff"), ("", [], "a body and a diff")],
)
def test_intent_waits_for_the_pull_request_body_and_diff(body: str, files: list[str], missing: str) -> None:
    def decide(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("an incomplete pull request was judged")

    state = {"pull_request_title": "T", "pull_request_body": body, "changed_files": files}
    answer = intent.judged(state, decide)
    reason = f"the pull request has no {missing} yet; the intent check judges it once it has both"
    assert answer == {"verdict": intent.UNCHECKED, "reason": reason, "stage": "delivered"}


@pytest.mark.parametrize(
    ("recorded", "recorded_repo", "pull"),
    [
        ("", "", cli.PullHead("someone-else", "https://github.com/o/r")),
        (MINE, "", cli.PullHead("", "")),
        (MINE, "https://github.com/O/R.git", cli.PullHead(MINE, "https://github.com/o/r")),
        (MINE, "https://github.com/o/bundle", cli.PullHead("someone-else", "https://github.com/o/r")),
    ],
)
def test_swarm_pr_keeps_a_pull_request_it_cannot_tell_apart_from_the_task_branch(
    env: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    recorded: str,
    recorded_repo: str,
    pull: cli.PullHead,
) -> None:
    row = _started(env, monkeypatch, pull)
    row.update(branch=recorded, branch_repo=recorded_repo)
    assert run("sw", "--as", "engineer@a1b2c3-0001", "pr", URL) == 0
    assert row["pr_url"] == URL


@pytest.mark.parametrize(
    "recorded_repo", ["", "https://github.com/o/r", "https://github.com/o/r.git", "https://github.com/O/R"]
)
def test_swarm_pr_refuses_another_branch_of_the_same_repository(
    env: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    recorded_repo: str,
) -> None:
    row = _started(env, monkeypatch, cli.PullHead("someone-else", "https://github.com/o/r", 3, "abc123"))
    row["branch_repo"] = recorded_repo
    capsys.readouterr()
    assert run("sw", "--as", "engineer@a1b2c3-0001", "pr", URL) != 0
    refusal = f"pull request {URL} is on branch someone-else, not this task's recorded branch {MINE}"
    assert capsys.readouterr().err.strip().endswith(refusal)
    assert not row.get("pr_url")


@pytest.mark.parametrize("declared", [["--stage", "the gate"], ["--prerequisite", "the tests"]])
def test_a_declared_pull_request_may_sit_on_another_branch(
    env: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    declared: list[str],
) -> None:
    row = _started(env, monkeypatch, cli.PullHead("someone-else", "", 3, "abc123"))
    assert run("sw", "--as", "engineer@a1b2c3-0001", "pr", URL, *declared) == 0
    assert row["pr_url"] == URL


def test_a_split_task_half_may_sit_on_another_branch(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    row = _started(env, monkeypatch, cli.PullHead("someone-else", "", 3, "abc123"))
    row["split"] = [{"what": "first"}, {"what": "second"}]
    env[1].state = lambda slug: {"overview": "", "phases": [], "tasks": [row]}
    assert run("sw", "--as", "engineer@a1b2c3-0001", "pr", URL, "--half", "1") == 0
    assert row["split_pulls"] == [URL]


def test_a_prerequisite_after_the_main_pull_request_keeps_its_number_and_head(
    env: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _started(env, monkeypatch, cli.PullHead(MINE, "", 3, "abc123"))
    assert run("sw", "--as", "engineer@a1b2c3-0001", "pr", URL) == 0
    monkeypatch.setattr(cli, "pull_branch", lambda url: cli.PullHead("someone-else", "", 9, "fff999"))
    other = "https://github.com/o/r/pull/9"
    assert run("sw", "--as", "engineer@a1b2c3-0001", "pr", other, "--prerequisite", "x") == 0
    assert (row["pr_url"], row["branch"], row["pr_number"], row["pr_head"]) == (URL, MINE, 3, "abc123")


def test_a_pull_request_without_a_number_stores_no_confirmation(
    env: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _started(env, monkeypatch, cli.PullHead(MINE, ""))
    assert run("sw", "--as", "engineer@a1b2c3-0001", "pr", URL) == 0
    assert "pr_number" not in row and "pr_head" not in row


def test_the_pull_branch_keeps_the_fork_repository_url() -> None:
    reference = ChangeRef("o/r", 3)
    change = ChangeState(reference, "open", "abc123", "dev", False, "u", MINE, "", False, "fork/r")
    found = cli.pull_branch(URL, lambda url: (FakeSourceControl({"change": change}), reference))
    assert found == cli.PullHead(MINE, "https://github.com/fork/r", 3, "abc123")


def test_intent_judges_a_pull_request_holding_a_body_and_a_diff() -> None:
    assert intent.unready({"pull_request_body": "Closes 1", "changed_files": ["a.py"]}) == ""
    assert intent.unready({"stage": "plan"}) == ""
