import json
import subprocess

import pytest

from scripts.doctor import ci, ci_read
from tests.doctor.recorded import load


@pytest.mark.parametrize("reverse", [False, True])
def test_cancelled_manual_failure_is_quiet_when_the_same_workflow_head_passed(reverse):
    record = _read(reverse=reverse)
    assert ci.red_checks(record, 1_791_300_000_000) == []


@pytest.mark.parametrize(
    "change",
    [
        {"conclusion": "failure"},
        {"status": "in_progress"},
        {"workflow_id": 2},
        {"head_sha": "another-head"},
    ],
)
def test_cancelled_failure_stays_red_without_a_completed_same_workflow_head_success(change):
    record = _read(success_change=change)
    [finding] = ci.red_checks(record, 1_791_300_000_000)
    assert finding.measure == 1


def test_completed_failure_stays_red_even_when_another_run_passed():
    record = _read(cancelled_conclusion="failure")
    [finding] = ci.red_checks(record, 1_791_300_000_000)
    assert finding.measure == 1


@pytest.mark.parametrize("suite", [None, 300])
def test_checks_outside_the_cancelled_suite_stay_red(suite):
    record = _read(check_suite=suite)
    [finding] = ci.red_checks(record, 1_791_300_000_000)
    assert finding.measure == 1


@pytest.mark.parametrize("missing", ["workflow_id", "check_suite_id"])
def test_missing_run_identity_keeps_the_failed_check(missing):
    record = _read(missing=missing)
    [finding] = ci.red_checks(record, 1_791_300_000_000)
    assert finding.measure == 1


def _read(*, reverse=False, success_change=None, cancelled_conclusion="cancelled", check_suite=200, missing=None):
    fixture = load("ci")
    success = {**fixture["runs"][0], "workflow_id": 1, "check_suite_id": 100, **(success_change or {})}
    cancelled = {
        **fixture["runs"][0],
        "id": 42,
        "event": "workflow_dispatch",
        "conclusion": cancelled_conclusion,
        "workflow_id": 1,
        "check_suite_id": 200,
    }
    check = {**fixture["checks"][0], "conclusion": "failure", "check_suite": {"id": 200}}
    if check_suite is None:
        check.pop("check_suite")
    else:
        check["check_suite"] = {"id": check_suite}
    if missing:
        cancelled.pop(missing)
        success.pop(missing)
    runs = [cancelled, success] if reverse else [success, cancelled]
    root = "repos/the-cloud-clockwork/agentihooks"
    sha = fixture["pr"]["head"]["sha"]
    responses = {
        f"{root}/pulls/487": json.dumps(fixture["pr"]),
        f"{root}/commits/{sha}/check-runs?per_page=100": json.dumps(check),
        f"{root}/actions/runs?head_sha={sha}&per_page=100": "\n".join(json.dumps(run) for run in runs),
    }

    def gh(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, responses[argv[2]], "")

    return ci_read.pull_request("the-cloud-clockwork/agentihooks", 487, run=gh)
