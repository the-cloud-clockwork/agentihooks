"""Swarm pr records only a pull request GitHub confirms for the task's branch, and intent waits for its body and diff."""

from __future__ import annotations

import inspect
import os
from collections.abc import Callable, Mapping
from email.message import Message
from typing import Any, NoReturn
from urllib.error import HTTPError

import pytest

from scripts.forge.gitlab import Transport, urlopen_transport
from scripts.forge.remote import ChangeLink
from scripts.gates import intent
from scripts.swarm import cli, pull_head
from scripts.swarm.store import SwarmError
from scripts.swarm_ledger import ledger_tasks
from scripts.tick.adapters.fakes import FakeSourceControl
from scripts.tick.ports.source_control import ChangeRef, ChangeState
from tests.forge.fakes import Sent
from tests.helpers.seat import SLUG, seat_agent
from tests.swarm.test_cli import env, run  # noqa: F401

URL = "https://github.com/o/r/pull/3"
REPO = "https://github.com/o/r"
MINE = "engineer-a1b2c3-0001"
GITLAB = "https://gitlab.example.com/group/sub/project/-/merge_requests/7"
GITLAB_API = "https://gitlab.example.com/api/v4/projects/group%2Fsub%2Fproject/merge_requests/7"
GITLAB_REPO = "https://gitlab.example.com/group/sub/project"
GITLAB_HEAD = "5f0c2d1e9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d"
TOKEN = {"GITLAB_TOKEN": "fake-token"}


def _started(env: Any, monkeypatch: pytest.MonkeyPatch, pull: cli.PullHead) -> dict[str, Any]:  # noqa: F811
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    seat_agent(store, ledger, SLUG, "t1")
    seat_agent(store, ledger, SLUG, "t2")
    ledger.rows["t1"]["branch"] = MINE
    monkeypatch.setattr(cli, "pull_branch", lambda url: pull)
    return ledger.rows["t1"]


def _ports(state: str, branch: str = MINE, body: str = "") -> Callable[[str], tuple[FakeSourceControl, ChangeRef]]:
    reference = ChangeRef("o/r", 3)
    change = ChangeState(reference, state, "abc123", "dev", False, "u", branch, body, False, "")
    return lambda url: (FakeSourceControl({"change": change}), reference)


def merge_request(state: str = "opened", body: str | None = None, source: int = 77) -> dict[str, Any]:
    """Return a merge request answer shaped like the GitLab REST v4 API, from project 77 unless ``source`` names a
    fork."""
    return {
        "iid": 7,
        "state": state,
        "sha": GITLAB_HEAD,
        "source_branch": MINE,
        "target_branch": "dev",
        "source_project_id": source,
        "target_project_id": 77,
        "web_url": GITLAB,
        "draft": False,
        "description": body,
    }


def gitlab(send: Transport, environ: Mapping[str, str] = TOKEN) -> Callable[[ChangeLink], tuple[str, cli.PullHead]]:
    """Return the GitLab forge pull_branch takes, reading through ``send`` with the token in ``environ``."""
    return lambda link: pull_head.gitlab_change(link, send, environ)


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
    head = "0123456789abcdef0123456789abcdef01234567"
    row = _started(env, monkeypatch, cli.PullHead(MINE, "", 3, head))
    assert run("sw", "--as", "engineer@a1b2c3-0001", "pr", URL) == 0
    assert (row["pr_url"], row["branch"], row["pr_number"], row["pr_head"]) == (URL, MINE, 3, head)
    stored = {"pr_number": row["pr_number"], "pr_head": row["pr_head"]}
    ledger_tasks.check(
        {"op": "task_update", "id": "x", "by": "engineer@a1b2c3-0001", "item": "tasks/t1", "fields": stored}
    )


def test_the_pull_branch_returns_the_number_and_head_of_an_open_pull_request() -> None:
    assert pull_head.pull_branch(URL, _ports("open")) == cli.PullHead(MINE, "", 3, "abc123")


@pytest.mark.feature("swarm-pr-closes-only-named-issues")
def test_the_pull_branch_returns_the_body_of_the_pull_request() -> None:
    assert pull_head.pull_branch(URL, _ports("open", body="Closes #9")) == cli.PullHead(
        MINE, "", 3, "abc123", "Closes #9"
    )


@pytest.mark.feature("swarm-pr-closes-only-named-issues")
def test_swarm_pr_refuses_a_body_closing_an_issue_the_task_never_names(
    env: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    row = _started(env, monkeypatch, cli.PullHead(MINE, "", 3, "abc123", "Closes #5\nCloses #9"))
    row.update(issue_url=f"{REPO}/issues/5", description="Fix the crash")
    capsys.readouterr()
    assert run("sw", "--as", "engineer@a1b2c3-0001", "pr", URL) != 0
    refusal = (
        f"pull request {URL} closes {REPO}/issues/9, which this task neither records nor names in its description: "
        "drop the closing keyword from the body, or record the task's own issue with swarm issue"
    )
    assert capsys.readouterr().err.strip().endswith(refusal)
    assert not row.get("pr_url") and row["state"] != "pr"


@pytest.mark.feature("swarm-pr-closes-only-named-issues")
def test_swarm_pr_records_a_body_closing_issues_the_task_and_its_group_name(
    env: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _started(env, monkeypatch, cli.PullHead(MINE, "", 3, "abc123", "Closes #5, closes #7, closes #9"))
    row.update(issue_url=f"{REPO}/issues/5", description="Also fixes #7", group_members=["t2"])
    rows = env[1].rows
    rows["t2"]["issue_url"] = f"{REPO}/issues/9"
    reads: list[tuple[str, str]] = []
    monkeypatch.setattr(env[1], "task", lambda slug, task_id: reads.append((slug, task_id)) or rows.get(task_id))
    assert run("sw", "--as", "engineer@a1b2c3-0001", "pr", URL) == 0
    assert (row["pr_url"], row["state"]) == (URL, "pr")
    assert ("sw", "t2") in reads and {slug for slug, _ in reads} == {"sw"}


@pytest.mark.feature("swarm-pr-closes-only-named-issues")
def test_swarm_pr_records_a_prerequisite_without_checking_its_closing_keywords(
    env: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    final = "https://github.com/o/r/pull/1"
    row = _started(env, monkeypatch, cli.PullHead(MINE, "", 3, "abc123", "Closes #9"))
    row.update(issue_url=f"{REPO}/issues/5", pr_url=final)
    assert run("sw", "--as", "engineer@a1b2c3-0001", "pr", URL, "--prerequisite", "the schema") == 0
    assert (row["pr_url"], row["pull_scopes"][URL]["kind"]) == (final, "prerequisite")


def test_the_pull_branch_refuses_a_closed_pull_request() -> None:
    with pytest.raises(SwarmError) as refused:
        pull_head.pull_branch(URL, _ports("closed"))
    assert str(refused.value) == f"pull request {URL} is closed, not open"


def test_the_pull_branch_refuses_a_pull_request_github_does_not_confirm() -> None:
    def ports(url: str) -> NoReturn:
        raise KeyError("no such pull request")

    with pytest.raises(SwarmError) as refused:
        pull_head.pull_branch(URL, ports)
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
    reason = f"the pull request lacks {missing}; the intent check judges it once it has both"
    assert answer == {"verdict": intent.FAIL, "reason": reason, "stage": "delivered"}


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
    ("recorded_repo", "pull_repo"),
    [
        ("", "https://github.com/o/r"),
        ("https://github.com/o/r", "https://github.com/o/r"),
        ("https://github.com/o/r.git", "https://github.com/o/r"),
        ("https://github.com/O/R", "https://github.com/o/r"),
        ("https://github.com/o/r", ""),
    ],
)
def test_swarm_pr_refuses_another_branch_of_the_same_repository(
    env: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    recorded_repo: str,
    pull_repo: str,
) -> None:
    row = _started(env, monkeypatch, cli.PullHead("someone-else", pull_repo, 3, "abc123"))
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
    found = pull_head.pull_branch(URL, lambda url: (FakeSourceControl({"change": change}), reference))
    assert found == cli.PullHead(MINE, "https://github.com/fork/r", 3, "abc123")


def test_intent_judges_a_pull_request_holding_a_body_and_a_diff() -> None:
    assert intent.unready({"pull_request_body": "Closes 1", "changed_files": ["a.py"]}) == ""
    assert intent.unready({"stage": "plan"}) == ""


@pytest.mark.feature("ledger-task-update-takes-confirmed-pull-request")
def test_the_pull_branch_confirms_a_gitlab_merge_request_through_the_forge() -> None:
    sent = Sent(merge_request(body="Adds the reader."))
    found = pull_head.pull_branch(GITLAB, forge=gitlab(sent))
    assert found == cli.PullHead(MINE, GITLAB_REPO, 7, GITLAB_HEAD, "Adds the reader.")
    assert [(method, url, headers["PRIVATE-TOKEN"]) for method, url, headers, _ in sent.calls] == [
        ("GET", GITLAB_API, "fake-token")
    ]


@pytest.mark.feature("ledger-task-update-takes-confirmed-pull-request")
def test_the_pull_branch_reads_the_head_project_of_a_gitlab_fork() -> None:
    sent = Sent(merge_request(source=91), {"id": 91, "path_with_namespace": "fork/project"})
    found = pull_head.pull_branch(GITLAB, forge=gitlab(sent))
    assert found == cli.PullHead(MINE, "https://gitlab.example.com/fork/project", 7, GITLAB_HEAD, "")
    assert sent.calls[1][:2] == ("GET", "https://gitlab.example.com/api/v4/projects/91")


@pytest.mark.feature("ledger-task-update-takes-confirmed-pull-request")
def test_the_pull_branch_refuses_a_merge_request_gitlab_does_not_confirm() -> None:
    def missing(method: str, url: str, headers: Mapping[str, str], data: bytes | None) -> NoReturn:
        raise HTTPError(url, 404, "Not Found", Message(), None)

    with pytest.raises(SwarmError) as refused:
        pull_head.pull_branch(GITLAB, forge=gitlab(missing))
    assert str(refused.value) == f"GitLab does not confirm merge request {GITLAB}: HTTP Error 404: Not Found"


@pytest.mark.feature("ledger-task-update-takes-confirmed-pull-request")
def test_the_pull_branch_refuses_a_gitlab_link_without_a_token() -> None:
    sent = Sent()
    with pytest.raises(SwarmError) as refused:
        pull_head.pull_branch(GITLAB, forge=gitlab(sent, {}))
    assert str(refused.value) == f"GitLab does not confirm merge request {GITLAB}: GITLAB_TOKEN is not set"
    assert sent.calls == []


@pytest.mark.feature("ledger-task-update-takes-confirmed-pull-request")
def test_the_pull_branch_refuses_a_closed_merge_request() -> None:
    with pytest.raises(SwarmError) as refused:
        pull_head.pull_branch(GITLAB, forge=gitlab(Sent(merge_request(state="closed"))))
    assert str(refused.value) == f"pull request {GITLAB} is closed, not open"


@pytest.mark.feature("ledger-task-update-takes-confirmed-pull-request")
def test_the_pull_branch_reads_a_github_link_without_the_gitlab_forge() -> None:
    def forge(link: ChangeLink) -> NoReturn:
        raise AssertionError(link)

    assert pull_head.pull_branch(URL, _ports("open"), forge) == cli.PullHead(MINE, "", 3, "abc123")


@pytest.mark.feature("ledger-task-update-takes-confirmed-pull-request")
def test_the_gitlab_forge_reads_the_network_with_the_process_environment_by_default() -> None:
    reader = inspect.signature(pull_head.gitlab_change).parameters
    assert reader["send"].default is urlopen_transport
    assert reader["env"].default is os.environ
    assert inspect.signature(pull_head.pull_branch).parameters["forge"].default is pull_head.gitlab_change
