from importlib import import_module
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.gates import log
from scripts.review.record import Finding, Record
from scripts.swarm import ready
from tests.review.fakes import HEAD, REVIEWER, SLUG, URL, store

AUTHOR = "engineer@a1b2c3-0001"


@pytest.mark.feature("review-queue-gate")
@pytest.mark.parametrize("recovering", [False, True])
@pytest.mark.parametrize("mode", ["observe", "enforce"])
@pytest.mark.parametrize("reviewed", [False, True])
def test_ready_first_entries_apply_the_review_gate_to_the_actual_head_and_claimant(
    monkeypatch: pytest.MonkeyPatch, recovering: bool, mode: str, reviewed: bool
) -> None:
    gate = import_module("scripts.review.gate")
    metadata = store()
    monkeypatch.setattr(metadata, "config", lambda slug: SimpleNamespace(gates={"review": mode}))
    reviewed_record = Record(URL, HEAD, (Finding("f1", "Finding", "comment", REVIEWER, "r1", "read", "", (AUTHOR,)),))
    reads: list[tuple[str, str, str]] = []
    rows: list[log.Row] = []
    queued: list[str] = []

    def read(found: Any, slug: str, url: str, head: str) -> Record | None:
        assert found is metadata
        reads.append((slug, url, head))
        return reviewed_record if reviewed else None

    def queue(action: str, url: str, **kwargs: Any) -> dict[str, Any]:
        kwargs["admit"](HEAD)
        queued.append(url)
        return {}

    monkeypatch.setattr(gate.record, "read", read)
    monkeypatch.setattr(log, "append", lambda slug, row: rows.append(row))
    monkeypatch.setattr(ready, "_journal", lambda *args: None)
    monkeypatch.setattr(ready, "_priority", lambda *args: None)
    monkeypatch.setattr(ready, "_reopen", lambda *args: ["blocked"])
    monkeypatch.setattr(ready, "_retry", lambda *args: ["blocked"])
    ledger = SimpleNamespace(raise_alert=lambda *args: None, notice=lambda *args: None)
    fresh = SimpleNamespace(head=HEAD, mergeable="MERGEABLE", red=False, merge_problem="", resolved=True)
    run = ready.Pass(metadata, SLUG, ledger, lambda url: fresh, lambda url: fresh, 1000, queue)
    row = {"id": "t1", "claimed_by": AUTHOR}
    found = {"url": URL, "on": {"head": HEAD}, "retry_at": 0, "recoveries": 0}
    messages = ready._recover(run, row, found) if recovering else ready._queue(run, row, found)
    refused = not reviewed and mode == "enforce"
    assert queued == ([] if refused else [URL])
    assert messages == (["blocked"] if refused else [f"queued {URL} for task t1"])
    assert reads == [(SLUG, URL, HEAD)]
    assert [(entry.gate, entry.kind, entry.agent, entry.task) for entry in rows] == (
        [] if reviewed else [("review", "deny" if refused else "observe", "merge watcher", "t1")]
    )


@pytest.mark.feature("review-queue-gate")
@pytest.mark.parametrize("route", ["worker", "expert", "hotfix", "delegated"])
@pytest.mark.parametrize("mode", ["observe", "enforce"])
@pytest.mark.parametrize(
    "scenario", ["absent", "unread", "other-reader", "self-agent", "self-task", "read", "missing-author"]
)
def test_each_command_caller_checks_the_task_authors_independent_read(
    monkeypatch: pytest.MonkeyPatch, route: str, mode: str, scenario: str
) -> None:
    from scripts.swarm import cli, merge_queue, queue_exit
    from scripts.swarm.ledger_client import LedgerClient
    from scripts.swarm.store import AgentRecord
    from tests.swarm.test_merge_queue import swarm_of

    gate = import_module("scripts.review.gate")
    master = route in {"hotfix", "delegated"}
    caller = AgentRecord(
        "master@a1b2c3-0002" if master else AUTHOR,
        "master" if master else ("expert" if route == "expert" else "eng"),
        "master" if master else "t1",
    )
    metadata = swarm_of(caller.lane)
    metadata.agents = lambda slug: [caller]
    metadata.config = lambda slug: SimpleNamespace(gates={"review": mode})
    monkeypatch.setattr(cli, "connect", lambda: metadata)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", caller.name)
    monkeypatch.setattr(cli, "_hotfix_master", lambda *args: route == "hotfix")
    monkeypatch.setattr(cli.master_queue, "route", lambda *args: {"task": "t1"} if route == "delegated" else None)
    monkeypatch.setattr(cli.done_gate, "require_local", lambda *args: None)
    monkeypatch.setattr(cli, "_require_target", lambda *args: None)
    monkeypatch.setattr(cli, "_queue_intent", lambda *args: None)
    monkeypatch.setattr(queue_exit, "require_fresh", lambda *args: None)
    owner = "" if scenario == "missing-author" else AUTHOR
    row = {"id": "t1", "claimed_by": owner, "pr_url": URL}
    monkeypatch.setattr(LedgerClient, "task", lambda *args: row)
    monkeypatch.setattr(LedgerClient, "tasks_targeting", lambda *args: [row])
    by = AUTHOR if scenario == "self-agent" else REVIEWER
    task = "t1" if scenario == "self-task" else "r1"
    state = "open" if scenario == "unread" else "read"
    readers = (
        (caller.name,)
        if scenario in {"other-reader", "missing-author"} and master
        else (("another-agent",) if scenario == "other-reader" else (AUTHOR,))
    )
    found = (
        None
        if scenario == "absent"
        else Record(URL, HEAD, (Finding("f1", "Finding", "comment", by, task, state, "", readers),))
    )
    rows: list[log.Row] = []
    queued: list[str] = []
    monkeypatch.setattr(gate.record, "read", lambda *args: found)
    monkeypatch.setattr(log, "append", lambda slug, entry: rows.append(entry))

    def queue(action: str, url: str, **kwargs: Any) -> dict[str, Any]:
        kwargs["admit"](HEAD)
        queued.append(url)
        return {}

    monkeypatch.setattr(merge_queue, "operate", queue)
    accepted = scenario == "read" or (scenario == "missing-author" and not master)
    refused = mode == "enforce" and not accepted
    assert cli.main([SLUG, "merge", "queue", URL]) == int(refused)
    assert queued == ([] if refused else [URL])
    assert [(entry.agent, entry.task, entry.kind) for entry in rows] == (
        [] if accepted else [(caller.name, caller.task, "deny" if refused else "observe")]
    )
