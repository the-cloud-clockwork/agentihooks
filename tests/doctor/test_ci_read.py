import copy
import json
import subprocess

import pytest

from scripts.doctor import ci, ci_read
from tests.doctor.recorded import load


def test_gh_reader_reads_all_pages_and_attempts_and_excludes_other_prs():
    fixture = load("ci")
    run = copy.deepcopy(fixture["runs"][0])
    run["run_attempt"] = 2
    unrelated = {**run, "id": 42, "head_branch": "other-pr", "pull_requests": [{"number": 999}]}
    node = fixture["attempts"][0]["tests"][0]["nodeid"]
    calls = []

    def gh(argv, **kwargs):
        calls.append(argv)
        assert kwargs["check"] is True
        assert "--method" not in argv
        endpoint = argv[2]
        if endpoint.endswith("/pulls/487"):
            output = json.dumps(fixture["pr"])
        elif "/check-runs?" in endpoint:
            output = "\n".join(json.dumps(c) for c in fixture["checks"])
        elif "/attempts/" in endpoint:
            number = int(endpoint.split("/attempts/")[1].split("/")[0])
            output = json.dumps(
                {"id": number, "name": "unit (3.11, 1)", "status": "completed", "conclusion": "success"}
            )
        elif endpoint.endswith("/logs"):
            number = int(endpoint.split("/jobs/")[1].split("/")[0])
            outcome = "FAILED" if number == 1 else "PASSED"
            output = f"2026-10-05T00:00:00Z [gw0] {outcome} {node}\n"
        else:
            output = "\n".join(json.dumps(r) for r in (unrelated, run))
        return subprocess.CompletedProcess(argv, 0, output, "")

    record = ci_read.pull_request("the-cloud-clockwork/agentihooks", 487, run=gh)
    assert len(record["checks"]) == 10
    assert [r["id"] for r in record["runs"]] == [37323835080]
    assert [a["attempt"] for a in record["attempts"]] == [1, 2]
    assert len(ci.flaky_tests(record)) == 1
    assert ci.reruns(record)[0].measure == 1
    assert all("--paginate" in c and "--jq" in c for c in calls if c[1] == "api" and "?" in c[2])


def test_gh_error_propagates_instead_of_reporting_healthy():
    def unavailable(argv, **kwargs):
        raise subprocess.CalledProcessError(1, argv, stderr="API unavailable")

    with pytest.raises(subprocess.CalledProcessError):
        ci_read.pull_request("the-cloud-clockwork/agentihooks", 487, run=unavailable)


def test_parameter_names_with_spaces_keep_their_identity():
    record = load("ci")
    sha = record["pr"]["head"]["sha"]
    first = "tests/test_example.py::test_case[case one]"
    second = "tests/test_example.py::test_case[case two]"
    failed = ci_read.test_results(f"2026-10-05T00:00:00Z [gw0] FAILED {first}\n", "unit")
    passed = ci_read.test_results(f"2026-10-05T00:00:00Z {second} PASSED [100%]\n", "unit")
    assert failed[0]["nodeid"] == first
    assert passed[0]["nodeid"] == second
    record["attempts"] = [
        {"run_id": 1, "attempt": 1, "head_sha": sha, "tests": failed},
        {"run_id": 1, "attempt": 2, "head_sha": sha, "tests": passed},
    ]
    assert ci.flaky_tests(record) == []
    record["attempts"][1]["tests"] = ci_read.test_results(f"{first} PASSED [100%]\n", "unit")
    [found] = ci.flaky_tests(record)
    assert first in found.evidence
