import json
import subprocess

import pytest

from scripts import ci_queue_rebuild as rebuild

pytestmark = pytest.mark.unit
PULL = "https://github.com/org/repo/pull/"
STARTED = "2026-10-16T01:00:00Z"


def group(number, position, *, head=None, enqueued="2026-10-16T00:30:00Z", jump=False, enqueuer="nestorcolt"):
    return {
        "pull": PULL + str(number),
        "position": position,
        "state": "QUEUED",
        "jump": jump,
        "enqueued_at": enqueued,
        "enqueuer": enqueuer,
        "head": head or f"h{number}",
        "base": "b",
    }


def added(at, actor="nestorcolt"):
    return {"__typename": "AddedToMergeQueueEvent", "createdAt": at, "actor": {"login": actor}}


def removed(at, reason="manual", actor="nestorcolt"):
    return {"__typename": "RemovedFromMergeQueueEvent", "createdAt": at, "actor": {"login": actor}, "reason": reason}


def test_an_entry_enqueued_ahead_after_the_group_started_is_the_linked_cause():
    groups = [group(1, 1), group(3541, 2, enqueued="2026-10-16T01:16:43Z", jump=True), group(3505, 3)]
    assert rebuild.cause(PULL + "3505", STARTED, groups, []) == {
        "action": "priority enqueue",
        "ahead": [
            {"pull": PULL + "3541", "enqueuer": "nestorcolt", "enqueued_at": "2026-10-16T01:16:43Z", "jump": True}
        ],
    }


def test_a_plain_enqueue_ahead_is_named_as_an_enqueue():
    groups = [group(7, 1, enqueued="2026-10-16T01:05:00Z"), group(3505, 2)]
    assert rebuild.cause(PULL + "3505", STARTED, groups, [])["action"] == "enqueue ahead"


def test_this_pull_requests_own_removal_names_its_actor_and_reason():
    events = [added("2026-10-16T00:30:00Z"), removed("2026-10-16T01:10:00Z", reason="manual", actor="ops")]
    assert rebuild.cause(PULL + "3505", STARTED, [group(1, 1)], events) == {
        "action": "dequeue",
        "actor": "ops",
        "reason": "manual",
        "at": "2026-10-16T01:10:00Z",
    }


def test_a_removal_then_add_of_this_pull_request_is_a_requeue():
    events = [removed("2026-10-16T01:16:41Z"), added("2026-10-16T01:16:43Z", actor="ops")]
    groups = [group(1, 1), group(3505, 2, enqueued="2026-10-16T01:16:43Z")]
    assert rebuild.cause(PULL + "3505", STARTED, groups, events) == {
        "action": "requeue",
        "actor": "ops",
        "removed_reason": "manual",
        "at": "2026-10-16T01:16:43Z",
    }


def test_events_before_the_group_started_and_unordered_delivery_link_nothing():
    events = [removed("2026-10-16T00:10:00Z"), added("2026-10-16T00:20:00Z"), added("2026-10-16T00:15:00Z")]
    found = rebuild.cause(PULL + "3505", STARTED, [group(1, 1), group(3505, 2)], events)
    assert found["action"] == "unknown"
    assert "base push" in found["gap"]


def test_a_pull_request_gone_without_a_removal_event_is_unknown():
    found = rebuild.cause(PULL + "3505", STARTED, [group(1, 1)], [])
    assert found["action"] == "unknown"
    assert "left the queue" in found["gap"]


class Gh:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        if self.fail:
            return subprocess.CompletedProcess(command, 1, "", "HTTP 403")
        if command[2] != "graphql":
            assert command == ["gh", "api", "repos/org/repo/actions/runs/7"]
            return subprocess.CompletedProcess(command, 0, json.dumps({"created_at": STARTED}), "")
        args = dict(item.split("=", 1) for item in command[4::2])
        assert args == {"query": rebuild.QUERY, "url": PULL + "3505"}
        nodes = [
            {
                "position": 1,
                "state": "QUEUED",
                "jump": True,
                "enqueuedAt": "2026-10-16T01:16:43Z",
                "enqueuer": {"login": "nestorcolt"},
                "headCommit": {"oid": "h3541"},
                "baseCommit": {"oid": "b"},
                "pullRequest": {"number": 3541, "url": PULL + "3541"},
            },
            {
                "position": 2,
                "state": "AWAITING_CHECKS",
                "jump": False,
                "enqueuedAt": "2026-10-16T00:30:00Z",
                "enqueuer": None,
                "headCommit": {"oid": "new"},
                "baseCommit": {"oid": "h3541"},
                "pullRequest": {"number": 3505, "url": PULL + "3505"},
            },
        ]
        data = {
            "resource": {
                "timelineItems": {"nodes": []},
                "repository": {"mergeQueue": {"entries": {"nodes": nodes}}},
            }
        }
        return subprocess.CompletedProcess(command, 0, json.dumps({"data": data}), "")


ENV = {
    "GITHUB_REPOSITORY": "org/repo",
    "GITHUB_RUN_ID": "7",
    "GITHUB_RUN_ATTEMPT": "2",
    "GITHUB_SHA": "old",
    "GITHUB_REF": "refs/heads/gh-readonly-queue/dev/pr-3505-base",
}


def test_the_record_keeps_old_and_new_group_heads_and_the_linked_cause():
    record = rebuild.record("", ENV, Gh(), now="2026-10-16T01:17:10Z")
    assert record["id"] == "queue-rebuild-7-2"
    assert record["pull"] == PULL + "3505"
    assert record["old_head"] == "old"
    assert record["observed_ref_head"] is None
    assert record["new_head"] == "new"
    assert record["new_base"] == "h3541"
    assert record["started"] == STARTED
    assert record["cause"]["action"] == "priority enqueue"
    assert [g["pull"] for g in record["groups"]] == [PULL + "3541", PULL + "3505"]


def test_an_unreadable_queue_still_yields_a_record_naming_the_gap():
    record = rebuild.record("other", ENV, Gh(fail=True), now="2026-10-16T01:17:10Z")
    assert record["observed_ref_head"] == "other"
    assert record["cause"] == {"action": "unknown", "gap": "queue unreadable: HTTP 403"}
    assert record["new_head"] is None


def test_main_prints_the_summary_logs_the_record_and_writes_the_job_summary(tmp_path, monkeypatch, capsys):
    summary = tmp_path / "summary.md"
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(rebuild.subprocess, "run", Gh())
    assert rebuild.main(["replacement"]) == 0
    out = capsys.readouterr()
    assert out.out == f"record queue-rebuild-7-2: priority enqueue of {PULL}3541 by nestorcolt\n"
    logged = json.loads(out.err.split("queue-rebuild ", 1)[1])
    assert logged["observed_ref_head"] == "replacement"
    assert "queue-rebuild-7-2" in summary.read_text()
    assert '"new_head": "new"' in summary.read_text()


@pytest.mark.parametrize(
    "found, expected",
    [
        ({"action": "dequeue", "actor": "ops", "reason": "manual", "at": "t"}, "dequeue by ops (manual)"),
        ({"action": "requeue", "actor": "ops", "removed_reason": "manual", "at": "t"}, "requeue by ops"),
        ({"action": "unknown", "gap": "g"}, "unknown cause: g"),
    ],
)
def test_the_summary_names_each_cause(found, expected):
    assert rebuild.summary({"id": "r", "cause": found}) == f"record r: {expected}"
