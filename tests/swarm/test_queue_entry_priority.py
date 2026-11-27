import argparse
import json
import subprocess
from email.message import Message
from types import SimpleNamespace
from typing import Any
from urllib.error import HTTPError

import pytest

from scripts.swarm import cli, merge_queue, queue_priority, ready
from scripts.swarm.store import SwarmError
from tests.swarm.test_merge_queue_priority import URL
from tests.swarm.test_queue_priority import priority

_fixture = priority
pytestmark = pytest.mark.unit


def ledger_for(
    rank: str, targets: list[str], frozen: tuple[str, ...] = (), others: tuple[dict[str, Any], ...] = ()
) -> SimpleNamespace:
    task = {"id": "t9", "phase": "p", "rank": rank, "lane": "eng", "pr_url": URL, "state": "pr"}

    def hierarchy(slug: str) -> list[dict[str, Any]]:
        if not targets:
            pytest.fail("the hierarchy is read without a focus")
        return (
            [
                {"node": "phases/p", "kind": "phase", "parent": "missions/m"},
                {"node": "missions/m", "kind": "mission", "parent": None},
            ]
            if slug == "sw"
            else []
        )

    return SimpleNamespace(
        tasks_targeting=lambda slug, url: [task, *others] if (slug, url) == ("sw", URL) else [],
        freezes=lambda slug: (
            [
                *({"verb": "focus", "target": target} for target in targets),
                *({"verb": "freeze", "target": target} for target in frozen),
            ]
            if slug == "sw"
            else []
        ),
        hierarchy=hierarchy,
        notice=lambda *args: None,
    )


@pytest.mark.feature("merge-priority-automatic-entry")
@pytest.mark.parametrize(
    ("rank", "targets", "prioritized"),
    [
        ("urgent", [], True),
        ("normal", ["tasks/t9"], True),
        ("normal", ["phases/p"], True),
        ("normal", ["missions/m"], True),
        ("normal", ["lane:eng"], True),
        ("normal", [], False),
        ("normal", ["phases/other"], False),
    ],
)
def test_queue_entry_applies_focus_or_urgency_without_interrupting_tests(
    priority: SimpleNamespace, rank: str, targets: list[str], prioritized: bool
) -> None:
    ledger = ledger_for(rank, targets)
    result = merge_queue.operate(
        "queue",
        URL,
        run=priority.queue.run,
        priority=lambda: queue_priority.automatic(priority.store, "sw", URL, ledger),
    )
    assert result["queued"] is True
    expected = ["P1", "P9", "P2", "P3"] if prioritized else ["P1", "P2", "P3", "P9"]
    assert [entry["pullRequest"]["id"] for entry in priority.queue.entries] == expected
    assert ("dequeue", 1) not in priority.queue.mutations


@pytest.mark.feature("merge-priority-automatic-entry")
@pytest.mark.parametrize(("focus", "frozen"), [("phases/p", "tasks/t9"), ("missions/m", "phases/p")])
def test_a_freeze_nearer_than_the_focus_keeps_the_queue_order(
    priority: SimpleNamespace, focus: str, frozen: str
) -> None:
    ledger = ledger_for("normal", [focus], (frozen,))
    result = merge_queue.operate(
        "queue",
        URL,
        run=priority.queue.run,
        priority=lambda: queue_priority.automatic(priority.store, "sw", URL, ledger),
    )
    assert result["priority"] is None
    assert [entry["pullRequest"]["id"] for entry in priority.queue.entries] == ["P1", "P2", "P3", "P9"]


@pytest.mark.feature("merge-priority-automatic-entry")
def test_automatic_priority_tells_a_displaced_split_pull_owner_as_the_swarm(
    priority: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[tuple[str, str, str, dict[str, Any]]] = []

    def inbox(redis: Any) -> SimpleNamespace:
        assert redis is priority.store.redis
        return SimpleNamespace(
            send=lambda sender, recipient, text, **flags: sent.append((sender, recipient, text, flags))
        )

    monkeypatch.setattr(queue_priority, "InboxStore", inbox)
    split = {"id": "t2", "pr_url": URL[:-1] + "2", "split_pulls": [URL], "claimed_by": "eng@two", "state": "pr"}
    ledger = ledger_for("urgent", [], others=(split,))
    merge_queue.operate(
        "queue",
        URL,
        run=priority.queue.run,
        priority=lambda: queue_priority.automatic(priority.store, "sw", URL, ledger),
    )
    assert [entry["pullRequest"]["id"] for entry in priority.queue.entries] == ["P1", "P9", "P2", "P3"]
    assert sent == [
        (
            "swarm",
            "eng@two",
            f"Your pull request {URL[:-1]}2 was requeued in its original order after priority request {URL}.",
            {"fyi": True},
        )
    ]


@pytest.mark.feature("merge-priority-automatic-entry")
def test_an_existing_queue_entry_does_not_repeat_automatic_priority(priority: SimpleNamespace) -> None:
    priority.queue.entries.append(priority.queue.entry(9))
    callbacks = []
    result = merge_queue.operate("queue", URL, run=priority.queue.run, priority=lambda: callbacks.append(URL))
    assert result["queued"] is True
    assert callbacks == []
    assert priority.queue.mutations == []


@pytest.mark.feature("merge-priority-automatic-entry")
def test_a_busy_priority_leaves_the_new_entry_queued_and_names_the_refusal(priority: SimpleNamespace) -> None:
    priority.store.redis.set(priority.store.key("sw", queue_priority.ACTIVE), "elsewhere:1:x")
    result = merge_queue.operate(
        "queue",
        URL,
        run=priority.queue.run,
        priority=lambda: queue_priority.automatic(priority.store, "sw", URL, ledger_for("urgent", [])),
    )
    assert result["queued"] is True
    assert result["priority"] == {
        "url": URL,
        "errors": ["another merge queue priority is running; try again when it ends"],
    }
    assert [entry["pullRequest"]["id"] for entry in priority.queue.entries] == ["P1", "P2", "P3", "P9"]


@pytest.mark.feature("merge-priority-automatic-entry")
def test_a_repository_without_a_queue_does_not_apply_automatic_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = {
        "id": "P9",
        "baseRefName": "dev",
        "state": "MERGED",
        "headRefOid": "head9",
        "mergeQueueEntry": None,
        "repository": {"mergeQueue": None},
        "isDraft": False,
        "reviewDecision": "APPROVED",
    }
    monkeypatch.setattr(merge_queue, "graphql", lambda *args: {"resource": raw})
    monkeypatch.setattr(merge_queue.staged_compatibility, "require", lambda *args: None)
    monkeypatch.setattr(merge_queue, "settle", lambda *args: None)
    monkeypatch.setattr(merge_queue, "squash", lambda *args: "merge-head")
    callbacks = []
    result = merge_queue.operate("queue", URL, priority=lambda: callbacks.append(URL))
    assert result["queued"] is False
    assert result["merge_commit"] == "merge-head"
    assert callbacks == []


@pytest.mark.feature("merge-priority-automatic-entry")
def test_cli_queue_entry_supplies_automatic_priority(
    priority: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_hotfix_master", lambda *args: True)
    monkeypatch.setattr(cli, "LedgerClient", lambda: ledger_for("urgent", []))

    def operate(action: str, url: str, **kwargs: Any) -> dict:
        kwargs["priority"]()
        return {"queued": True}

    monkeypatch.setattr(merge_queue, "operate", operate)
    cli.cmd_merge(priority.store, argparse.Namespace(action="queue", slug="sw", url=URL))
    assert [entry["pullRequest"]["id"] for entry in priority.queue.entries] == ["P1", "P9", "P2", "P3"]


@pytest.mark.feature("merge-priority-automatic-entry")
@pytest.mark.parametrize("recovering", [False, True])
def test_watcher_queue_entry_supplies_automatic_priority(
    priority: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, recovering: bool
) -> None:
    def queue(action: str, url: str, **kwargs: Any) -> dict:
        kwargs["priority"]()
        return {"queued": True}

    fresh = SimpleNamespace(head="head9", mergeable="MERGEABLE", red=False, merge_problem="", resolved=True)
    run = ready.Pass(priority.store, "sw", ledger_for("urgent", []), lambda url: fresh, lambda url: fresh, 1000, queue)
    monkeypatch.setattr(ready, "save", lambda *args: None)
    monkeypatch.setattr(ready, "_journal", lambda *args: None)
    row = {"id": "t9"}
    found = {"url": URL, "on": {"head": "head9"}, "retry_at": 0, "recoveries": 0}
    messages = ready._recover(run, row, found) if recovering else ready._queue(run, row, found)
    assert messages == [f"queued {URL} for task t9"]
    assert [entry["pullRequest"]["id"] for entry in priority.queue.entries] == ["P1", "P9", "P2", "P3"]


def refused_hierarchy(slug: str) -> list[dict[str, Any]]:
    raise HTTPError(f"https://ledger/{slug}/hierarchy", 409, "Conflict", Message(), None)


@pytest.mark.feature("merge-queue-priority-failure-keeps-entry")
def test_an_accepted_entry_reports_a_refused_hierarchy_read_as_a_priority_error(priority: SimpleNamespace) -> None:
    ledger = ledger_for("normal", ["phases/p"])
    ledger.hierarchy = refused_hierarchy
    result = merge_queue.operate(
        "queue",
        URL,
        run=priority.queue.run,
        priority=lambda: queue_priority.automatic(priority.store, "sw", URL, ledger),
    )
    assert result["queued"] is True
    assert result["priority_error"] == "HTTP Error 409: Conflict"
    assert "priority" not in result
    assert [entry["pullRequest"]["id"] for entry in priority.queue.entries] == ["P1", "P2", "P3", "P9"]


@pytest.mark.feature("merge-queue-priority-failure-keeps-entry")
def test_cli_queue_prints_the_accepted_entry_and_exits_zero_when_its_priority_fails(
    priority: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = ledger_for("normal", ["phases/p"])
    ledger.hierarchy = refused_hierarchy
    monkeypatch.setattr(cli, "_hotfix_master", lambda *args: True)
    monkeypatch.setattr(cli, "LedgerClient", lambda: ledger)
    accepted = merge_queue.operate

    def operate(action: str, url: str, **kwargs: Any) -> dict[str, Any]:
        return accepted(action, url, run=priority.queue.run, priority=kwargs["priority"])

    monkeypatch.setattr(merge_queue, "operate", operate)
    assert cli.main(["sw", "merge", "queue", URL]) == 0
    printed = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert printed["queued"] is True
    assert printed["priority_error"] == "HTTP Error 409: Conflict"


@pytest.mark.feature("merge-queue-priority-failure-keeps-entry")
def test_a_refused_enqueue_still_fails_and_never_runs_its_priority(priority: SimpleNamespace) -> None:
    def refusing(command: list[str], **kwargs: Any) -> Any:
        if command[2] == "graphql" and command[4] == f"query={merge_queue.QUEUE}":
            return subprocess.CompletedProcess(command, 1, "", "GraphQL: Pull request is not mergeable\n")
        return priority.queue.run(command, **kwargs)

    callbacks: list[str] = []
    with pytest.raises(SwarmError) as caught:
        merge_queue.operate("queue", URL, run=refusing, priority=lambda: callbacks.append(URL))
    assert str(caught.value) == "GraphQL: Pull request is not mergeable"
    assert callbacks == []
    assert [entry["pullRequest"]["id"] for entry in priority.queue.entries] == ["P1", "P2", "P3"]
