import json
import subprocess

import pytest

from scripts.swarm import ledger_events, merge_queue
from scripts.swarm.store import SwarmError

URL = "https://github.com/o/r/pull/7"
FORBIDDEN = {
    "type": "FORBIDDEN",
    "path": ["resource", "statusCheckRollup"],
    "message": "Resource not accessible by integration",
}
READ_PULL = ledger_events.view
RULES = [{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "Gate — Required"}]}}]


class GitHub:
    def __init__(
        self, queue=None, states=("CLEAN",), rollup=False, merge_error=None, conclusions=("SUCCESS",), heads=("abc",)
    ):
        self.queue = queue
        self.conclusions = list(conclusions)
        self.heads = list(heads)
        self.commit = None
        self.states = list(states)
        self.rollup = rollup
        self.merge_error = merge_error
        self.merged = []
        self.enqueued = []

    def pull(self):
        state = "MERGED" if self.merged else "OPEN"
        entry = {"id": "MQ_one", "position": 1, "state": "QUEUED"} if self.enqueued else None
        merge_state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return {
            "id": "PR_one",
            "state": state,
            "headRefOid": self.heads.pop(0) if len(self.heads) > 1 else self.heads[0],
            "baseRefName": "dev",
            "mergeQueueEntry": entry,
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": merge_state,
            "reviewDecision": None,
            "repository": {"mergeQueue": self.queue},
            "body": "",
        }

    def answer(self, command, payload, code=0):
        return subprocess.CompletedProcess(command, code, json.dumps(payload), "" if code == 0 else "gh: failed")

    def checks(self, command):
        if not self.rollup:
            return self.answer(command, {"data": {"resource": {"state": "OPEN"}}, "errors": [FORBIDDEN]}, 1)
        conclusion = self.conclusions.pop(0) if len(self.conclusions) > 1 else self.conclusions[0]
        commit = {
            "id": "commit",
            "committedDate": "2026-10-24T15:00:00Z",
            "statusCheckRollup": {
                "contexts": {"nodes": [{"name": "Gate — Required", "conclusion": conclusion, "isRequired": True}]}
            },
            "checkSuites": {"nodes": [], "pageInfo": {"hasNextPage": False}},
        }
        resource = {"state": "OPEN", "headRefOid": "abc", "commits": {"nodes": [{"commit": commit}]}}
        return self.answer(command, {"data": {"resource": resource}})

    def __call__(self, command, **kwargs):
        assert kwargs == {"capture_output": True, "text": True, "timeout": 20}
        if command[2] == "repos/o/r/rules/branches/dev":
            return self.answer(command, RULES)
        query = command[4]
        if query == f"query={merge_queue.FRESHNESS}":
            resource = {"repository": {"nameWithOwner": "o/r"}, "baseRefName": "dev", "baseRef": None}
            return self.answer(command, {"data": {"resource": resource}})
        if query == f"query={merge_queue.STATE}":
            return self.answer(command, {"data": {"resource": self.pull()}})
        if query == f"query={merge_queue.MERGE}":
            if self.merge_error:
                return self.answer(command, {"data": None, "errors": [{"message": self.merge_error}]})
            self.merged.append(command[5:])
            return self.answer(
                command,
                {
                    "data": {
                        "mergePullRequest": {
                            "pullRequest": {"state": "MERGED", "mergeCommit": self.commit or {"oid": "m1"}}
                        }
                    }
                },
            )
        if query == f"query={merge_queue.QUEUE}":
            self.enqueued.append(command[5:])
            return self.answer(command, {"data": {"enqueuePullRequest": {"mergeQueueEntry": {"id": "MQ_one"}}}})
        if "statusCheckRollup" in query:
            return self.checks(command)
        raise AssertionError(f"unexpected call {command}")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(merge_queue.ledger_events, "view", READ_PULL)
    slept = []
    monkeypatch.setattr(merge_queue.time, "sleep", slept.append)
    return slept


def test_the_wait_polls_every_fifteen_seconds_for_forty_attempts():
    assert (merge_queue.POLL, merge_queue.ATTEMPTS) == (15, 40)


def test_the_state_query_reads_no_check_rollup():
    assert "statusCheckRollup" not in merge_queue.STATE
    assert "mergeStateStatus" in merge_queue.STATE


@pytest.mark.parametrize("status", ["CLEAN", "HAS_HOOKS", "UNSTABLE"])
def test_a_base_without_a_queue_squash_merges_when_the_rollup_is_unreadable_and_reports_the_merge_commit(status):
    github = GitHub(states=(status,))
    assert merge_queue.operate("queue", URL, github) == {
        "url": URL,
        "state": "MERGED",
        "head": "abc",
        "queued": False,
        "entry": None,
        "merge_commit": "m1",
    }
    assert github.merged == [["-f", "id=PR_one", "-f", "head=abc"]]
    assert github.enqueued == []


def test_a_base_without_a_queue_waits_until_the_merge_state_is_clean(no_sleep):
    github = GitHub(states=("BLOCKED", "BLOCKED", "BLOCKED", "CLEAN"))
    assert merge_queue.operate("queue", URL, github, attempts=5)["merge_commit"] == "m1"
    assert no_sleep == [merge_queue.POLL, merge_queue.POLL]
    assert len(github.merged) == 1


def test_a_base_without_a_queue_never_merges_while_checks_stay_blocked(no_sleep):
    github = GitHub(states=("BLOCKED",))
    with pytest.raises(SwarmError) as caught:
        merge_queue.operate("queue", URL, github, attempts=3)
    assert str(caught.value) == (
        "required checks on the current pull request head must pass before merging; merge state BLOCKED"
    )
    assert no_sleep == [merge_queue.POLL, merge_queue.POLL]
    assert github.merged == []


def test_a_base_without_a_queue_merges_on_a_readable_green_rollup():
    github = GitHub(states=("BLOCKED",), rollup=True)
    assert merge_queue.operate("queue", URL, github)["merge_commit"] == "m1"


def test_a_refused_direct_merge_names_the_missing_token_permission():
    github = GitHub(merge_error="Resource not accessible by integration")
    with pytest.raises(SwarmError) as caught:
        merge_queue.operate("queue", URL, github)
    assert str(caught.value) == (
        "squash merge refused: Resource not accessible by integration; "
        "the GitHub token needs the pull requests write permission"
    )


def test_a_base_with_a_queue_enqueues_and_never_merges():
    github = GitHub(queue={"id": "MQ_dev"}, rollup=True)
    result = merge_queue.operate("queue", URL, github)
    assert result["queued"] is True
    assert "merge_commit" not in result
    assert github.enqueued == [["-f", "id=PR_one", "-f", "head=abc"]]
    assert github.merged == []


def test_a_merge_without_a_reported_commit_reports_none():
    github = GitHub()
    github.commit = {}
    assert merge_queue.operate("queue", URL, github)["merge_commit"] is None


def test_a_base_without_a_queue_waits_for_pending_required_checks(no_sleep):
    github = GitHub(rollup=True, conclusions=(None, "SUCCESS"))
    assert merge_queue.operate("queue", URL, github, attempts=3)["merge_commit"] == "m1"
    assert no_sleep == [merge_queue.POLL]


def test_a_base_without_a_queue_never_merges_on_a_red_required_check():
    github = GitHub(rollup=True, conclusions=("FAILURE",))
    with pytest.raises(SwarmError) as caught:
        merge_queue.operate("queue", URL, github, attempts=3)
    assert str(caught.value) == (
        "required checks on the current pull request head must pass before queueing; failed: Gate — Required"
    )
    assert github.merged == []


def test_a_base_without_a_queue_refuses_a_moved_head():
    github = GitHub(states=("BLOCKED",), heads=("abc", "def"))
    with pytest.raises(SwarmError) as caught:
        merge_queue.operate("queue", URL, github, attempts=3)
    assert str(caught.value) == "the pull request head moved; merge the new head once its checks pass"
    assert github.merged == []
