import json
from types import SimpleNamespace

import pytest

from scripts.swarm import ready
from scripts.swarm.ledger_events import GATE, pull_request, view, views
from tests.swarm.test_ready import HEAD, URL, merging  # noqa: F401

SUITE = {
    "status": "COMPLETED",
    "conclusion": "SUCCESS",
    "workflowRun": {"databaseId": 1, "createdAt": "2026-10-15T15:00:00Z"},
}


def check(name, conclusion, required):
    return {"name": name, "conclusion": conclusion, "completedAt": "2026-10-15T15:05:00Z", "isRequired": required}


def head(gated, *checks):
    raw = {"state": "OPEN", "headRefOid": HEAD, "gated": gated, "commits": []}
    return pull_request({**raw, "statusCheckRollup": list(checks), "checkSuites": [SUITE]})


@pytest.mark.parametrize(
    "gated, checks",
    [
        (True, [check(GATE, "SUCCESS", True), check("measure", "FAILURE", False)]),
        (True, [check(GATE, "SUCCESS", False), check("measure", "FAILURE", False)]),
        (True, [{"context": GATE, "state": "SUCCESS", "isRequired": False}, check("measure", "FAILURE", False)]),
        (False, [check("unit", "SUCCESS", True), check("measure", "FAILURE", False)]),
        (False, [check("unit", "SUCCESS", True), {"context": "sonar", "state": "FAILURE", "isRequired": False}]),
    ],
)
def test_an_optional_check_failure_leaves_the_head_green(gated, checks):
    pull = head(gated, *checks)
    assert (pull.resolved, pull.red, pull.failed, pull.red_at) == (True, False, (), None)
    assert pull.gate_passed is gated


@pytest.mark.parametrize(
    "gated, checks, failed",
    [
        (True, [check(GATE, "FAILURE", True), check("unit", "FAILURE", False)], (GATE, "unit")),
        (False, [check("unit", "FAILURE", True), check("measure", "FAILURE", False)], ("unit", "measure")),
    ],
)
def test_a_required_check_failure_turns_the_head_red(gated, checks, failed):
    pull = head(gated, *checks)
    assert (pull.resolved, pull.red, pull.failed) == (True, True, failed)
    assert pull.red_at is not None


def test_an_ungated_head_without_required_checks_counts_every_check():
    pull = head(False, check("unit", "SUCCESS", False), check("measure", "FAILURE", False))
    assert (pull.resolved, pull.red, pull.failed) == (True, True, ("measure",))


def test_a_single_view_pages_its_contexts_with_the_pull_request_number():
    commit = {
        "id": "C1",
        "committedDate": "2026-10-15T15:00:00Z",
        "statusCheckRollup": {"contexts": {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": "c"}}},
        "checkSuites": {"nodes": [SUITE], "pageInfo": {}},
    }
    first = {"data": {"resource": {"state": "OPEN", "headRefOid": HEAD, "commits": {"nodes": [{"commit": commit}]}}}}
    page = {
        "data": {"node": {"statusCheckRollup": {"contexts": {"nodes": [check(GATE, "SUCCESS", True)], "pageInfo": {}}}}}
    }
    replies, calls = [first, page], []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps(replies[len(calls) - 1]))

    assert view("https://github.com/o/r/pull/42/", run).red is False
    assert "isRequired(pullRequestNumber:42)" in calls[0][4] and "isRequired(pullRequestNumber:42)" in calls[1][4]


def test_a_url_without_a_pull_number_asks_for_number_zero():
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"data": {"p0": None}}))

    assert views(["https://github.com/o/r/issues"], run) == {"https://github.com/o/r/issues": None}
    assert "isRequired(pullRequestNumber:0)" in calls[0][-1]


def test_an_optional_failure_beside_a_skipped_gate_names_no_failed_check():
    pull = head(True, check(GATE, "SKIPPED", True), check("measure", "FAILURE", False))
    assert (pull.unpassed_gate, pull.red, pull.failed) == (GATE, True, ())


def test_an_optional_failure_before_the_gate_reports_leaves_the_head_running():
    raw = {"state": "OPEN", "headRefOid": HEAD, "gated": True, "commits": []}
    running = {**SUITE, "status": "IN_PROGRESS", "conclusion": None}
    pull = pull_request({**raw, "statusCheckRollup": [check("measure", "FAILURE", False)], "checkSuites": [running]})
    assert (pull.resolved, pull.red, pull.failed, pull.unpassed_gate) == (False, False, (), "")


def test_the_merge_pass_queues_past_an_optional_failure(merging):  # noqa: F811
    merging.pulls[URL] = head(True, check(GATE, "SUCCESS", True), check("measure", "FAILURE", False))
    assert merging.run() == [f"queued {URL} for task t1"]
    assert merging.queued == [("queue", URL)]
    assert merging.rows["t1"]["state"] == "pr"


def test_the_merge_pass_reopens_on_a_required_failure(merging):  # noqa: F811
    merging.pulls[URL] = head(True, check(GATE, "FAILURE", True), check("unit", "FAILURE", False))
    assert merging.run()[0] == f"task t1 reopened, {URL} has red checks: {GATE}, unit"
    assert merging.queued == []
    assert ready.record(merging.store, "sw", "t1") is None
