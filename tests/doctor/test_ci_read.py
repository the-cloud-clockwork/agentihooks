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


@pytest.mark.parametrize("current_red", [False, True], ids=["fresh-head-without-checks", "red-current-head"])
def test_red_checks_use_only_the_current_pull_request_head(current_red):
    fixture = load("ci")
    old_sha = fixture["pr"]["head"]["sha"]
    fresh_sha = "a" * 40
    old_check = {**fixture["checks"][0], "conclusion": "failure"}
    fresh_check = {**old_check, "head_sha": fresh_sha}
    pr = {**fixture["pr"], "head": {**fixture["pr"]["head"], "sha": fresh_sha}}
    workflow = {**fixture["runs"][0], "head_sha": fresh_sha}
    root = "repos/the-cloud-clockwork/agentihooks"
    responses = {
        f"{root}/pulls/487": json.dumps(pr),
        f"{root}/commits/{old_sha}/check-runs?per_page=100": json.dumps(old_check),
        f"{root}/commits/{fresh_sha}/check-runs?per_page=100": json.dumps(fresh_check) if current_red else "",
        f"{root}/actions/runs?head_sha={fresh_sha}&per_page=100": json.dumps(workflow) if current_red else "",
    }

    def gh(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, responses[argv[2]], "")

    record = ci_read.pull_request("the-cloud-clockwork/agentihooks", 487, run=gh)
    found = ci.red_checks(record, 1_791_300_000_000)
    if current_red:
        assert record["checks"] == [fresh_check]
        assert [f.id for f in found] == ["red-checks/487"]
        assert found[0].measure == 1
    else:
        assert record["checks"] == []
        assert found == []


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
    assert len(cache.get(key).encode()) < 1024 * 1024
    assert 3590 <= cache.ttl(key) <= 3600
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


def test_a_job_whose_log_was_removed_reads_no_tests_and_keeps_the_other_jobs():
    workflow = {**load("ci")["runs"][0], "run_attempt": 2, "status": "in_progress"}

    def gh(argv, **kwargs):
        endpoint = argv[2]
        if "/attempts/" in endpoint:
            jobs = [
                {"id": 1, "name": "sonar", "status": "completed", "conclusion": "failure"},
                {"id": 2, "name": "unit", "status": "completed", "conclusion": "failure"},
            ]
            return subprocess.CompletedProcess(argv, 0, "\n".join(json.dumps(job) for job in jobs), "")
        if endpoint == "repos/o/r/actions/jobs/1/logs":
            raise subprocess.CalledProcessError(1, argv, "<Code>BlobNotFound</Code>", "gh: HTTP 404\n")
        return subprocess.CompletedProcess(argv, 0, "2026-10-05T00:00:00Z FAILED tests/test_x.py::test_x\n", "")

    assert ci_read._attempts("o/r", [workflow], gh)[0]["tests"] == [
        {"job": "unit", "nodeid": "tests/test_x.py::test_x", "outcome": "FAILED"}
    ]


def test_a_log_read_that_fails_for_another_reason_still_fails_the_detector():
    workflow = {**load("ci")["runs"][0], "run_attempt": 2, "status": "in_progress"}

    def gh(argv, **kwargs):
        if "/attempts/" in argv[2]:
            job = {"id": 1, "name": "unit", "status": "completed", "conclusion": "failure"}
            return subprocess.CompletedProcess(argv, 0, json.dumps(job), "")
        raise subprocess.CalledProcessError(1, argv, "", "gh: HTTP 502\n")

    with pytest.raises(subprocess.CalledProcessError):
        ci_read._attempts("o/r", [workflow], gh)


RUNS_JQ = ".workflow_runs[] | {id, name, path, conclusion, created_at, html_url} | @json"
DEV_PUSHES = "repos/{owner}/{repo}/actions/runs?branch=dev&event=push&status=completed&per_page=100"
LAST_GREEN = (
    "repos/{owner}/{repo}/actions/workflows/hive-worker.yml/runs?branch=dev&event=push&status=success&per_page=1"
)


@pytest.mark.parametrize("found_green", [True, False], ids=["green found", "no green"])
def test_dev_pushes_reads_the_last_green_and_failing_jobs_of_each_red_workflow_only(found_green):
    hive = ".github/workflows/hive-worker.yml"
    runs = [
        {"id": n, "name": "Worker hive pod", "path": hive, "conclusion": "failure", "created_at": "t", "html_url": "u"}
        for n in range(10, 15)
    ] + [{"id": 40, "name": "Pages", "path": ".github/workflows/pages.yml", "conclusion": "success"}]
    green = {"id": 9, "name": "Worker hive pod", "path": hive, "conclusion": "success"}
    jobs = [
        {"name": "pod", "conclusion": "failure"},
        {"name": "contracts", "conclusion": "timed_out"},
        {"name": "lint", "conclusion": "success"},
        {"name": "image", "conclusion": "skipped"},
    ]
    calls = []

    def gh(argv, **kwargs):
        calls.append(argv[2])
        assert kwargs == {"cwd": "/repo", "capture_output": True, "text": True, "check": True, "timeout": 60}
        jq = ".jobs[] | {name, conclusion} | @json" if "/jobs?" in argv[2] else RUNS_JQ
        assert argv == ["gh", "api", argv[2], "--jq", jq]
        replies = {
            DEV_PUSHES: runs,
            LAST_GREEN: [green] if found_green else [],
            "repos/{owner}/{repo}/actions/runs/14/jobs?per_page=100": jobs,
        }
        return subprocess.CompletedProcess(argv, 0, "\n".join(json.dumps(r) for r in replies[argv[2]]), "")

    expected = {"runs": runs, "green": {hive: green} if found_green else {}, "jobs": {14: ["contracts", "pod"]}}
    assert ci_read.dev_pushes("/repo", gh) == expected
    assert len(calls) == 3


def test_dev_pushes_without_a_red_workflow_reads_only_the_run_list():
    calls = []

    def gh(argv, **kwargs):
        calls.append(argv[2])
        return subprocess.CompletedProcess(argv, 0, "", "")

    assert ci_read.dev_pushes("/repo", gh) == {"runs": [], "green": {}, "jobs": {}}
    assert calls == [DEV_PUSHES]
