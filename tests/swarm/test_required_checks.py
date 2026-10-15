import pytest

from scripts.swarm import ready
from scripts.swarm.ledger_events import GATE, pull_request
from tests.swarm.test_ready import HEAD, URL, merging  # noqa: F401

pytestmark = pytest.mark.xdist_group("fakeredis")

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
        (False, [check("unit", "SUCCESS", True), check("measure", "FAILURE", False)]),
    ],
)
def test_an_optional_check_failure_leaves_the_head_green(gated, checks):
    pull = head(gated, *checks)
    assert (pull.resolved, pull.red, pull.failed, pull.red_at) == (True, False, (), None)


@pytest.mark.parametrize(
    "gated, checks, failed",
    [
        (True, [check(GATE, "FAILURE", True), check("unit", "FAILURE", False)], (GATE, "unit")),
        (False, [check("unit", "FAILURE", True), check("measure", "FAILURE", False)], ("unit", "measure")),
        (False, [check("unit", "SUCCESS", False), check("measure", "FAILURE", False)], ("measure",)),
    ],
)
def test_a_required_check_failure_turns_the_head_red(gated, checks, failed):
    pull = head(gated, *checks)
    assert (pull.resolved, pull.red, pull.failed) == (True, True, failed)
    assert pull.red_at is not None


def test_an_optional_failure_beside_a_skipped_gate_names_no_failed_check():
    pull = head(True, check(GATE, "SKIPPED", True), check("measure", "FAILURE", False))
    assert (pull.unpassed_gate, pull.red, pull.failed) == (GATE, True, ())


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
