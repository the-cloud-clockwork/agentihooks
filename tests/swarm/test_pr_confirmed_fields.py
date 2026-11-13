"""Swarm pr on a confirmed pull request writes a task update the ledger accepts."""

from typing import Any

import pytest

from scripts.swarm import cli
from scripts.swarm_ledger import ledger_tasks
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_intent_cli import started  # noqa: F401

URL = "https://github.com/o/r/pull/11024"
HEAD = "0123456789abcdef0123456789abcdef01234567"


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
    monkeypatch.setattr(cli, "pull_branch", lambda url: ("engineer-a1b2c3-0001", "https://github.com/o/r", 11024, HEAD))
    assert run("sw", "pr", URL) == 0
    assert (sent[-1]["pr_number"], sent[-1]["pr_head"]) == (11024, HEAD)
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["pr_url"]) == ("pr", URL)
