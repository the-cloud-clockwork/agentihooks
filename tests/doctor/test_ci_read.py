import copy
import json
import re
import subprocess

import pytest

from scripts.doctor import ci, ci_read
from tests.doctor.recorded import load


@pytest.fixture(autouse=True)
def _cache_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("GH_TOKEN", "doctor-reader-test")
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))


def test_gh_reader_reads_all_pages_and_attempts_and_excludes_other_prs(monkeypatch):
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
    import time

    clock = [1_000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    record = ci_read.pull_request("the-cloud-clockwork/agentihooks", 487, run=gh, cache=cache)
    clock[0] += ci_read.github_reads.RUN_TTL_S
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


def test_doctor_run_and_check_reads_share_the_cache_and_charge_only_fetches(monkeypatch, tmp_path):
    import time

    from scripts.swarm import github_reads

    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    clock = [1_000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    fixture = load("ci")
    fixture["runs"][0]["run_attempt"] = 1
    calls = []

    def gh(argv, **kwargs):
        assert kwargs == {"capture_output": True, "text": True, "check": True, "timeout": 60}
        endpoint = argv[2]
        calls.append(endpoint)
        if "/pulls/" in endpoint:
            output = json.dumps(fixture["pr"])
        else:
            rows = fixture["checks"] if "/check-runs?" in endpoint else fixture["runs"]
            output = "\n".join(json.dumps(row) for row in rows)
        return subprocess.CompletedProcess(argv, 0, output, "")

    first = ci_read.pull_request("o/r", 487, run=gh)
    fixture["checks"][0]["conclusion"] = "failure"
    assert ci_read.pull_request("o/r", 487, run=gh) == first
    assert len(calls) == 4
    clock[0] += github_reads.RUN_TTL_S
    assert ci_read.pull_request("o/r", 487, run=gh)["checks"][0]["conclusion"] == "failure"
    assert len(calls) == 7
    assert github_reads.counts(0, clock[0] + 1) == {"doctor.ci": 7}
    rows = [json.loads(line) for line in (github_reads.home() / "charged.jsonl").read_text().splitlines()]
    assert [row["endpoint"] for row in rows] == calls


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
    assert len(calls) == 9


def test_job_logs_survive_attempt_cache_loss_and_concurrent_collectors(monkeypatch, tmp_path):
    import importlib
    import time
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    import fakeredis

    from scripts.swarm import github_reads

    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    clock = [1_000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    workflow = {**load("ci")["runs"][0], "run_attempt": 2, "status": "completed"}
    calls = []
    starting = Barrier(2)

    def gh(argv, **kwargs):
        assert kwargs == {"capture_output": True, "text": True, "check": True, "timeout": 60}
        endpoint = argv[2]
        calls.append(endpoint)
        if "/attempts/" in endpoint:
            number = int(endpoint.split("/attempts/")[1].split("/")[0])
            output = json.dumps({"id": number, "name": "unit", "status": "completed", "conclusion": "success"})
            if number == 1 and len([call for call in calls if "/attempts/1/" in call]) <= 2:
                starting.wait(timeout=10)
        else:
            time.sleep(0.02)
            output = "2026-10-05T00:00:00Z PASSED tests/test_x.py::test_x\n"
        return subprocess.CompletedProcess(argv, 0, output, "")

    def collect():
        return ci_read._attempts("o/r", [workflow], gh, fakeredis.FakeRedis(decode_responses=True))

    with ThreadPoolExecutor(max_workers=2) as collectors:
        first, second = list(collectors.map(lambda _: collect(), range(2)))
    assert first == second
    assert len([call for call in calls if call.endswith("/logs")]) == 2
    assert first[0]["tests"] == [{"job": "unit", "nodeid": "tests/test_x.py::test_x", "outcome": "PASSED"}]
    clock[0] += 7_200
    importlib.reload(github_reads)
    assert collect() == first
    assert len([call for call in calls if call.endswith("/logs")]) == 2
    assert github_reads.counts(0, clock[0] + 1)["doctor.ci.job_log"] == 2


def test_retained_logs_follow_the_gh_account_without_preference_invalidation(monkeypatch, tmp_path):
    from scripts.swarm import github_reads

    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    account = ["first-user"]
    host = ["github.com"]
    monkeypatch.delenv("GH_HOST", raising=False)

    def identity(command, **kwargs):
        assert command == ["gh", "config", "get", "user", "--host", host[0]]
        assert kwargs == {"capture_output": True, "text": True, "check": True, "timeout": 5}
        return subprocess.CompletedProcess(command, 0, account[0], "")

    monkeypatch.setattr(github_reads.subprocess, "run", identity)
    config = tmp_path / "gh"
    config.mkdir()
    preferences = config / "config.yml"
    preferences.touch()
    endpoint = "repos/o/r/actions/jobs/1/logs"
    logs = iter(["first", "second", "enterprise"])
    assert github_reads.job_log(endpoint, lambda: next(logs)) == "first"
    preferences.unlink()
    preferences.touch()
    assert github_reads.job_log(endpoint, lambda: next(logs)) == "first"
    account[0] = "second-user"
    assert github_reads.job_log(endpoint, lambda: next(logs)) == "second"
    assert github_reads.job_log(endpoint, lambda: next(logs)) == "second"

    host[0] = "github.example"
    monkeypatch.setenv("GH_HOST", host[0])
    assert github_reads.job_log(endpoint, lambda: next(logs)) == "enterprise"
    account[0] = ""
    with pytest.raises(ValueError, match="^GitHub account identity is unavailable$"):
        github_reads.job_log(endpoint, lambda: next(logs))


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


def _pulls_world(monkeypatch, tmp_path, numbers):
    import time

    import fakeredis

    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    clock = [1_000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    fixture = load("ci")
    fixture["runs"][0]["run_attempt"] = 1
    world = {
        "heads": {n: fixture["pr"]["head"]["sha"] for n in numbers},
        "checks": {n: "SUCCESS" for n in numbers},
        "calls": [],
        "cache": fakeredis.FakeRedis(decode_responses=True),
        "on_pull": lambda endpoint: None,
        "fixture": fixture,
    }

    def gh(argv, **kwargs):
        assert argv[:2] == ["gh", "api"]
        assert kwargs["check"] is True
        endpoint = argv[2]
        world["calls"].append(endpoint)
        if endpoint == "graphql":
            assert argv[3] == "-f"
            assert argv[4].startswith("query=")
            asked = re.findall(r'(p\d+): repository\(owner: "o", name: "r"\) \{ pullRequest\(number: (\d+)\)', argv[4])
            data = {
                alias: {"pullRequest": {"headRefOid": world["heads"][int(n)], "state": world["checks"][int(n)]}}
                for alias, n in asked
            }
            output = json.dumps({"data": data})
        elif "/pulls/" in endpoint:
            world["on_pull"](endpoint)
            number = int(endpoint.rsplit("/", 1)[1])
            head = {**fixture["pr"]["head"], "sha": world["heads"][number]}
            output = json.dumps({**fixture["pr"], "number": number, "head": head})
        else:
            field = (
                "jobs" if "/attempts/" in endpoint else "check_runs" if "/check-runs?" in endpoint else "workflow_runs"
            )
            assert argv[3:] == ["--paginate", "--jq", f".{field}[] | @json"]
            rows = {"jobs": [], "check_runs": fixture["checks"], "workflow_runs": fixture["runs"]}[field]
            output = "\n".join(json.dumps(row) for row in rows)
        return subprocess.CompletedProcess(argv, 0, output, "")

    def read_pass(**kwargs):
        world["calls"].clear()
        clock[0] += 1
        return ci_read.pull_requests([("o/r", n) for n in numbers], run=gh, cache=world["cache"], **kwargs)

    return world, read_pass


def test_an_unchanged_pull_request_costs_one_state_read_on_the_next_pass(monkeypatch, tmp_path):
    world, read_pass = _pulls_world(monkeypatch, tmp_path, [4, 5])
    first = read_pass()
    assert world["calls"].count("graphql") == 1
    assert [r["pr"]["number"] for r in first] == [4, 5]
    assert read_pass() == first
    assert world["calls"] == ["graphql"]


def test_a_new_head_reads_the_pull_request_in_full(monkeypatch, tmp_path):
    world, read_pass = _pulls_world(monkeypatch, tmp_path, [4, 5])
    read_pass()
    fresh = "b" * 40
    world["heads"][5] = fresh
    second = read_pass()
    assert world["calls"] == [
        "graphql",
        "repos/o/r/pulls/5",
        f"repos/o/r/commits/{fresh}/check-runs?per_page=100",
        f"repos/o/r/actions/runs?head_sha={fresh}&per_page=100",
    ]
    assert second[1]["pr"]["head"]["sha"] == fresh


def test_a_changed_check_state_on_the_same_head_reads_in_full(monkeypatch, tmp_path):
    world, read_pass = _pulls_world(monkeypatch, tmp_path, [4])
    read_pass()
    world["checks"][4] = "FAILURE"
    world["fixture"]["checks"][0]["conclusion"] = "failure"
    [record] = read_pass()
    assert world["calls"][:2] == ["graphql", "repos/o/r/pulls/4"]
    assert record["checks"][0]["conclusion"] == "failure"


@pytest.mark.parametrize(("count", "queries"), [(20, 1), (21, 2)])
def test_state_reads_beyond_twenty_pull_requests_split_into_more_queries(monkeypatch, tmp_path, count, queries):
    world, read_pass = _pulls_world(monkeypatch, tmp_path, list(range(1, count + 1)))
    read_pass(full_reads=0)
    assert world["calls"] == ["graphql"] * queries


def test_full_reads_per_pass_stop_at_the_bound_and_the_rest_wait_for_the_next_pass(monkeypatch, tmp_path):
    world, read_pass = _pulls_world(monkeypatch, tmp_path, [1, 2, 3])
    first = read_pass(full_reads=2)
    assert [r["pr"]["number"] for r in first] == [1, 2]
    assert sorted(c for c in world["calls"] if "/pulls/" in c) == ["repos/o/r/pulls/1", "repos/o/r/pulls/2"]
    old = first[0]["pr"]["head"]["sha"]
    for number in (1, 2):
        world["heads"][number] = "c" * 40
    second = read_pass(full_reads=1)
    assert [c for c in world["calls"] if "/pulls/" in c] == ["repos/o/r/pulls/3"]
    assert [(r["pr"]["number"], r["pr"]["head"]["sha"]) for r in second] == [(1, old), (2, old), (3, old)]
    read_pass(full_reads=1)
    assert [c for c in world["calls"] if "/pulls/" in c] == ["repos/o/r/pulls/1"]


def test_full_reads_of_one_pass_run_side_by_side(monkeypatch, tmp_path):
    import threading

    world, read_pass = _pulls_world(monkeypatch, tmp_path, [4, 5])
    barrier = threading.Barrier(2, timeout=5)
    world["on_pull"] = lambda endpoint: barrier.wait()
    assert [r["pr"]["number"] for r in read_pass()] == [4, 5]


def test_no_open_pull_request_reads_nothing():
    def gh(argv, **kwargs):
        pytest.fail(f"read without an open pull request: {argv}")

    assert ci_read.pull_requests([], run=gh, cache=None) == []


def test_the_state_read_asks_each_pull_request_head_and_check_states_in_one_query(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    queries = []

    def gh(argv, **kwargs):
        queries.append(argv[4])
        data = {"p0": {"pullRequest": None}, "p1": {"pullRequest": None}}
        return subprocess.CompletedProcess(argv, 0, json.dumps({"data": data}), "")

    states = ci_read._states([("o/r", 4), ("x/y", 7)], gh)
    assert states == ['{"pullRequest": null}', '{"pullRequest": null}']
    state = (
        "{ headRefOid commits(last: 1) { nodes { commit { checkSuites(first: 50) { nodes { databaseId status"
        " conclusion checkRuns(first: 100) { totalCount nodes { databaseId status conclusion startedAt } } } } } } } }"
    )
    assert queries == [
        f'query={{ p0: repository(owner: "o", name: "r") {{ pullRequest(number: 4) {state} }}'
        f' p1: repository(owner: "x", name: "y") {{ pullRequest(number: 7) {state} }} }}'
    ]


def test_a_full_read_is_kept_for_a_day_and_the_default_bound_is_eight(monkeypatch, tmp_path):
    numbers = list(range(1, 10))
    world, read_pass = _pulls_world(monkeypatch, tmp_path, numbers)
    assert [r["pr"]["number"] for r in read_pass()] == numbers[:8]
    assert world["cache"].ttl(f"{ci_read.PULL}o/r:1") == 86_400


def test_the_oldest_saved_read_is_refreshed_first(monkeypatch, tmp_path):
    world, read_pass = _pulls_world(monkeypatch, tmp_path, [1, 2])
    read_pass(full_reads=1)
    read_pass(full_reads=1)
    world["heads"][1] = "d" * 40
    read_pass(full_reads=1)
    world["heads"][1] = world["heads"][2] = "e" * 40
    read_pass(full_reads=1)
    assert [c for c in world["calls"] if "/pulls/" in c] == ["repos/o/r/pulls/2"]


def test_a_failed_read_keeps_the_other_reads_of_its_pass(monkeypatch, tmp_path):
    world, read_pass = _pulls_world(monkeypatch, tmp_path, [4, 5])

    def fail_five(endpoint):
        if endpoint.endswith("/5"):
            raise subprocess.CalledProcessError(1, "gh", stderr="HTTP 502")

    world["on_pull"] = fail_five
    with pytest.raises(subprocess.CalledProcessError):
        read_pass()
    world["on_pull"] = lambda endpoint: None
    read_pass()
    assert [c for c in world["calls"] if "/pulls/" in c] == ["repos/o/r/pulls/5"]


def test_a_full_read_keeps_each_retried_attempt_in_the_cache(monkeypatch, tmp_path):
    world, read_pass = _pulls_world(monkeypatch, tmp_path, [4])
    run = world["fixture"]["runs"][0]
    run["run_attempt"] = 2
    read_pass()
    attempts = [c for c in world["calls"] if "/attempts/" in c]
    assert attempts == [f"repos/o/r/actions/runs/{run['id']}/attempts/{n}/jobs?per_page=100" for n in (1, 2)]
    assert world["cache"].exists(*(f"{ci_read.ROOT}:doctor:ci-attempt:o/r:{run['id']}:{n}" for n in (1, 2))) == 2


def test_attempt_jobs_read_without_a_cache_are_never_shared(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    calls = []

    def gh(argv, **kwargs):
        calls.append(argv[2])
        return subprocess.CompletedProcess(argv, 0, "", "")

    workflow = {"id": 42, "head_sha": "head"}
    for _ in range(2):
        assert ci_read._attempt("o/r", workflow, 1, gh, None)["tests"] == []
    assert calls == ["repos/o/r/actions/runs/42/attempts/1/jobs?per_page=100"] * 2
