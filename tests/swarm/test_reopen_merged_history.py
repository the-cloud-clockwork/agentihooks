"""Reopening a done task moves its merged pull request to the task's history."""

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))

from scripts.swarm import done_gate, idle  # noqa: E402
from scripts.swarm.ledger_events import PullRequest  # noqa: E402
from scripts.swarm_ledger import ledger_tasks  # noqa: E402
from scripts.swarm_ledger.ledger_core import Context  # noqa: E402
from tests.gates.test_claim_stop import ME, MERGED, SLUG, rig  # noqa: E402, F401
from tests.helpers.seat import seat_agent  # noqa: E402
from tests.swarm.test_cli import env, run  # noqa: E402, F401

OLD = "https://github.com/o/r/pull/7"
NEW = "https://github.com/o/r/pull/8"


def done_ledger() -> dict[str, Any]:
    """A ledger holding one done code task whose pull request merged."""
    task = {"id": "t1", "title": "a", "lane": "eng", "kind": "code", "state": "done", "done": True}
    return {"tasks": [{**task, "claimed_by": ME, "pr_url": OLD}]}


def update(doc: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
    """Apply one task update the way the ledger server does and return the task."""
    op = {"op": "task_update", "id": "u1", "by": "master@demo", "item": "tasks/t1", "fields": fields}
    ledger_tasks.check(op)
    ledger_tasks.apply(doc, op, Context({"rev": 0, "stamps": {}}, 1))
    return doc["tasks"][0]


def read(pulls: dict[str, str]) -> Callable[[str], PullRequest | None]:
    """A GitHub reader answering from a map of pull request states."""
    return lambda url: None if url not in pulls else PullRequest(pulls[url], 1, 1, False, True)


@pytest.mark.feature("reopen-moves-merged-pull-request-to-history")
def test_reopening_a_done_task_moves_its_merged_pull_request_to_history() -> None:
    task = update(done_ledger(), {"state": "open", "claimed_by": ""})
    assert (task["state"], task["pr_url"], task["past_pulls"]) == ("open", "", [OLD])


@pytest.mark.feature("reopen-moves-merged-pull-request-to-history")
def test_a_reopen_clearing_its_pull_request_still_keeps_it_in_history() -> None:
    task = update(done_ledger(), {"state": "open", "claimed_by": "", "pr_url": ""})
    assert (task["pr_url"], task["past_pulls"]) == ("", [OLD])


@pytest.mark.feature("reopen-moves-merged-pull-request-to-history")
def test_swarm_done_on_a_reopened_task_refuses_until_its_new_pull_request_merges(
    env: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store, ledger, _ = env
    ledger.rows["t1"].update(update(done_ledger(), {"state": "open", "claimed_by": ""}))
    ledger.pulls[OLD], ledger.pulls[NEW] = MERGED, PullRequest("OPEN", None, 1, False)
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    seat_agent(store, ledger, "sw", "t1")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    capsys.readouterr()
    assert run("sw", "done") == 1
    assert run("sw", "done", "--pr", OLD) == 1
    assert run("sw", "done", "--pr", NEW) == 1
    assert capsys.readouterr().err.splitlines() == [
        "swarm: a code task is done only with its merged pull request: give --pr <url>",
        f"swarm: pull request {OLD} belongs to this task before it reopened; give the new pull request with --pr <url>",
        f"swarm: pull request {NEW} is open, not merged; merge it, then run swarm done again",
    ]
    assert ledger.rows["t1"]["state"] != "done"


@pytest.mark.feature("reopen-moves-merged-pull-request-to-history")
def test_a_reopen_naming_its_pull_request_keeps_it_current() -> None:
    task = update(done_ledger(), {"state": "open", "claimed_by": "", "pr_url": OLD})
    assert (task["pr_url"], task.get("past_pulls", [])) == (OLD, [])


@pytest.mark.feature("reopen-moves-merged-pull-request-to-history")
def test_the_swarm_reopens_an_unmerged_done_task_with_its_pull_request_kept() -> None:
    import fakeredis

    from scripts.swarm.store import RedisStore

    class Ledger:
        def __init__(self) -> None:
            self.doc = done_ledger()

        def update_task(self, slug: str, task_id: str, fields: dict[str, Any], **_: object) -> None:
            update(self.doc, fields)

        def comment(self, slug: str, task_id: str, text: str, by: str) -> None:
            pass

    ledger, store = Ledger(), RedisStore(fakeredis.FakeRedis(decode_responses=True))
    events = [{"kind": "task done", "target": "tasks/t1", "by": ME, "at": 1}]
    doc = {"tasks": [dict(ledger.doc["tasks"][0])], "_meta": {"events": events}}
    done_gate.recheck_pass(store, SLUG, doc, ledger, 2, read({OLD: "CLOSED"}))
    task = ledger.doc["tasks"][0]
    assert (task["state"], task["pr_url"], task.get("past_pulls", [])) == ("open", OLD, [])


@pytest.mark.feature("reopen-moves-merged-pull-request-to-history")
def test_a_reopened_task_stops_on_a_wait_and_done_refuses_until_the_new_pull_request_merges(rig: Any) -> None:  # noqa: F811
    reopened = update(done_ledger(), {"state": "open", "claimed_by": ""})
    rig.task.clear()
    rig.task.update(reopened, state="claimed", claimed_by=ME)
    rig.pulls[OLD] = MERGED
    idle.declare_wait(rig.store.redis, SLUG, ME, rig.clock[0] + 60_000, "branch preflight", rig.clock[0])
    assert rig.stop().allowed
    task = rig.task
    assert done_gate.refusal(task, task["pr_url"], read({OLD: "MERGED"})) == (
        "a code task is done only with its merged pull request: give --pr <url>"
    )
    assert done_gate.refusal(task, OLD, read({OLD: "MERGED"})) == (
        f"pull request {OLD} belongs to this task before it reopened; give the new pull request with --pr <url>"
    )
    assert "is open, not merged" in done_gate.refusal(task, NEW, read({NEW: "OPEN"}))
    assert done_gate.refusal(task, NEW, read({NEW: "MERGED"})) == ""
