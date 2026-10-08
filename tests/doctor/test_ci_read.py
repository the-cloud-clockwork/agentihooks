import copy
import json
import subprocess

import pytest

from scripts.doctor import ci, ci_read
from tests.doctor.recorded import load

pytestmark = pytest.mark.xdist_group("fakeredis")


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
        assert argv[:2] == ["gh", "api"]
        endpoint = argv[2]
        assert endpoint.startswith("repos/the-cloud-clockwork/agentihooks/")
        if endpoint.endswith("/pulls/487"):
            output = json.dumps(fixture["pr"])
        elif "/check-runs?" in endpoint:
            output = "\n".join(json.dumps(c) for c in fixture["checks"])
        elif "/attempts/" in endpoint:
            assert argv[3:] == ["--paginate", "--jq", ".jobs[] | @json"]
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

    import fakeredis

    cache = fakeredis.FakeRedis(decode_responses=True)
    record = ci_read.pull_request("the-cloud-clockwork/agentihooks", 487, run=gh, cache=cache)
    first_calls = len(calls)
    fixture["checks"][0]["conclusion"] = "failure"
    refreshed = ci_read.pull_request("the-cloud-clockwork/agentihooks", 487, run=gh, cache=cache)
    assert refreshed["checks"][0]["conclusion"] == "failure"
    assert refreshed["attempts"] == record["attempts"]
    assert len(calls) - first_calls == 3
    assert calls[first_calls][2] == "repos/the-cloud-clockwork/agentihooks/pulls/487"
    assert "/check-runs?" in calls[first_calls + 1][2]
    assert "/actions/runs?head_sha=" in calls[first_calls + 2][2]
    assert len(record["checks"]) == 10
    assert "committed_at" not in record
    assert [r["id"] for r in record["runs"]] == [37323835080]
    assert [a["attempt"] for a in record["attempts"]] == [1, 2]
    assert len(ci.flaky_tests(record)) == 1
    assert ci.reruns(record) == []
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


def test_single_attempt_workflows_do_not_download_logs():
    workflow = {**load("ci")["runs"][0], "run_attempt": 1}

    def gh(argv, **kwargs):
        pytest.fail(f"single attempt requested logs: {argv}")

    assert ci_read._attempts("o/r", [workflow], gh) == []


def test_completed_attempts_are_reused_while_new_attempts_are_read():
    import fakeredis

    cache = fakeredis.FakeRedis(decode_responses=True)
    workflow = {**load("ci")["runs"][0], "run_attempt": 2, "status": "completed"}
    calls = []

    def gh(argv, **kwargs):
        assert argv[:2] == ["gh", "api"]
        assert kwargs["check"] is True
        endpoint = argv[2]
        calls.append(endpoint)
        if "/attempts/" in endpoint:
            assert argv[3:] == ["--paginate", "--jq", ".jobs[] | @json"]
            number = int(endpoint.split("/attempts/")[1].split("/")[0])
            assert endpoint == f"repos/o/r/actions/runs/{workflow['id']}/attempts/{number}/jobs?per_page=100"
            output = json.dumps({"id": number, "name": "unit", "status": "completed", "conclusion": "success"})
        else:
            number = int(endpoint.split("/jobs/")[1].split("/")[0])
            assert endpoint == f"repos/o/r/actions/jobs/{number}/logs"
            outcome = "FAILED" if number == 1 else "PASSED"
            output = f"2026-10-05T00:00:00Z {outcome} tests/test_x.py::test_x\n"
        return subprocess.CompletedProcess(argv, 0, output, "")

    single = {**workflow, "id": 99, "run_attempt": 1}
    first = ci_read._attempts("o/r", [single, workflow], gh, cache)
    assert [a["attempt"] for a in first] == [1, 2]
    assert first[0]["tests"][0]["outcome"] == "FAILED"
    assert first[1]["tests"][0]["outcome"] == "PASSED"
    assert len(calls) == 4
    key = f"{ci_read.ROOT}:doctor:ci-attempt:o/r:{workflow['id']}:1"
    assert json.loads(cache.get(key)) == first[0]
    assert 604790 <= cache.ttl(key) <= 604800
    assert ci_read._attempts("o/r", [workflow], gh, cache) == first
    assert len(calls) == 4
    workflow["run_attempt"] = 3
    workflow["status"] = "in_progress"
    assert ci_read._attempts("o/r", [workflow], gh, cache) == first
    assert len(calls) == 4
    workflow["status"] = "completed"
    third = ci_read._attempts("o/r", [workflow], gh, cache)
    assert third[:2] == first
    assert third[2] == {
        "run_id": workflow["id"],
        "attempt": 3,
        "head_sha": workflow["head_sha"],
        "tests": [{"job": "unit", "nodeid": "tests/test_x.py::test_x", "outcome": "PASSED"}],
    }
    assert len(calls) == 6
    cache.flushall()
    assert ci_read._attempts("o/r", [workflow], gh, cache) == third
    assert len(calls) == 12


def test_attempt_reader_downloads_only_completed_unskipped_jobs():
    workflow = {**load("ci")["runs"][0], "run_attempt": 2, "status": "in_progress"}
    calls = []

    def gh(argv, **kwargs):
        assert argv[:2] == ["gh", "api"]
        endpoint = argv[2]
        calls.append(endpoint)
        if "/attempts/" in endpoint:
            assert endpoint == f"repos/o/r/actions/runs/{workflow['id']}/attempts/1/jobs?per_page=100"
            jobs = [
                {"id": 1, "name": "queued", "status": "in_progress", "conclusion": None},
                {"id": 2, "name": "skipped", "status": "completed", "conclusion": "skipped"},
                {"id": 3, "name": "unit", "status": "completed", "conclusion": "failure"},
            ]
            output = "\n".join(json.dumps(job) for job in jobs)
        else:
            assert endpoint == "repos/o/r/actions/jobs/3/logs"
            output = "2026-10-05T00:00:00Z FAILED tests/test_x.py::test_x\n"
        return subprocess.CompletedProcess(argv, 0, output, "")

    assert ci_read._attempts("o/r", [workflow], gh) == [
        {
            "run_id": workflow["id"],
            "attempt": 1,
            "head_sha": workflow["head_sha"],
            "tests": [{"job": "unit", "nodeid": "tests/test_x.py::test_x", "outcome": "FAILED"}],
        }
    ]
    assert len(calls) == 2
