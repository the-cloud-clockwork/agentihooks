"""Swarm pr on a confirmed pull request writes a task update the ledger accepts."""

from typing import Any

import pytest

from scripts.swarm import cli, pull_head
from scripts.swarm_ledger import ledger_tasks
from scripts.tick.adapters.fakes import FakeSourceControl
from scripts.tick.ports.source_control import ChangeRef, ChangeState
from tests.forge.fakes import Sent
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_intent_cli import started  # noqa: F401
from tests.swarm.test_pull_confirm import GITLAB, GITLAB_HEAD, GITLAB_REPO, merge_request

URL = "https://github.com/o/r/pull/11024"
HEAD = "0123456789abcdef0123456789abcdef01234567"
BRANCH = "engineer-a1b2c3-0001"
FIELDS = ("state", "branch", "branch_repo", "pr_number", "pr_head", "pr_url")


@pytest.mark.feature("ledger-task-update-takes-confirmed-pull-request")
def test_swarm_pr_on_a_confirmed_pull_request_passes_the_ledger_task_update_check(
    started: tuple[Any, Any, list],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, ledger, _ = started
    update = ledger.update_task
    sent: list[dict] = []

    def checked(slug: str, task: str, fields: dict, by: str = "swarm", *args: Any, **kwargs: Any) -> Any:
        ledger_tasks.check({"op": "task_update", "id": "x", "by": by, "item": f"tasks/{task}", "fields": fields})
        sent.append(fields)
        return update(slug, task, fields, by, *args, **kwargs)

    ledger.update_task = checked

    def confirmed(url: str) -> tuple[str, str, int, str]:
        return "engineer-a1b2c3-0001", "https://github.com/o/r", 11024, HEAD

    monkeypatch.setattr(cli, "pull_branch", confirmed)
    assert run("sw", "pr", URL) == 0
    assert (sent[-1]["pr_number"], sent[-1]["pr_head"]) == (11024, HEAD)
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["pr_url"]) == ("pr", URL)


def _checking(ledger: Any) -> list[dict[str, Any]]:
    """Make ``ledger`` run the ledger task update check on every update, and return the fields each update sent."""
    update = ledger.update_task
    sent: list[dict[str, Any]] = []

    def checked(slug: str, task: str, fields: dict[str, Any], by: str = "swarm", *args: Any, **kwargs: Any) -> Any:
        ledger_tasks.check({"op": "task_update", "id": "x", "by": by, "item": f"tasks/{task}", "fields": fields})
        sent.append(fields)
        return update(slug, task, fields, by, *args, **kwargs)

    ledger.update_task = checked
    return sent


@pytest.mark.feature("ledger-task-update-takes-confirmed-pull-request")
def test_swarm_pr_records_every_confirmed_field_of_a_github_pull_request(
    started: tuple[Any, Any, list],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, ledger, _ = started
    sent = _checking(ledger)
    change = ChangeState(ChangeRef("o/r", 11024), "open", HEAD, "dev", False, URL, BRANCH, "", False, "o/r")

    def ports(url: str) -> tuple[FakeSourceControl, ChangeRef]:
        return FakeSourceControl({"change": change}), change.reference

    monkeypatch.setattr(cli, "pull_branch", lambda url: pull_head.pull_branch(url, ports))
    assert run("sw", "pr", URL) == 0
    assert set(sent[-1]) == {*FIELDS, "awaiting"}
    assert {key: sent[-1][key] for key in FIELDS} == {
        "state": "pr",
        "branch": BRANCH,
        "branch_repo": "https://github.com/o/r",
        "pr_number": 11024,
        "pr_head": HEAD,
        "pr_url": URL,
    }


@pytest.mark.feature("ledger-task-update-takes-confirmed-pull-request")
@pytest.mark.parametrize("body", [None, "Closes #9"])
def test_swarm_pr_records_a_gitlab_merge_request_the_ledger_accepts(
    started: tuple[Any, Any, list],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    body: str | None,
) -> None:
    _, ledger, _ = started
    sent = _checking(ledger)
    transport = Sent(merge_request(body=body))
    monkeypatch.setenv("GITLAB_TOKEN", "fake-token")

    def confirmed(url: str) -> pull_head.PullHead:
        return pull_head.pull_branch(url, forge=lambda link: pull_head.gitlab_change(link, transport))

    monkeypatch.setattr(cli, "pull_branch", confirmed)
    assert run("sw", "pr", GITLAB) == 0
    assert {key: sent[-1][key] for key in FIELDS} == {
        "state": "pr",
        "branch": BRANCH,
        "branch_repo": GITLAB_REPO,
        "pr_number": 7,
        "pr_head": GITLAB_HEAD,
        "pr_url": GITLAB,
    }
