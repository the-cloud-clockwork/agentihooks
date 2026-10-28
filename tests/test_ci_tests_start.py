import json
import subprocess

import pytest


def _verify(monkeypatch, runs, jobs):
    from scripts import ci_tests_start

    calls = []

    def api(*args, **kwargs):
        endpoint = args[0][-1]
        calls.append(endpoint)
        payload = jobs if "/jobs?" in endpoint else {"workflow_runs": runs}
        return subprocess.CompletedProcess(args[0], 0, json.dumps(payload), "")

    monkeypatch.setattr(ci_tests_start.subprocess, "run", api)
    monkeypatch.setattr(ci_tests_start.time, "sleep", lambda _: None)
    return ci_tests_start.verify("owner/repo", "head", attempts=2, interval=0), calls


@pytest.mark.parametrize(
    "runs,jobs,message",
    [
        ([], {"total_count": 0}, "missing"),
        (
            [{"id": 11, "head_sha": "head", "status": "completed", "conclusion": "failure"}],
            {"total_count": 0},
            "failure",
        ),
        (
            [{"id": 11, "head_sha": "head", "status": "completed", "conclusion": "success"}],
            {"total_count": 0},
            "no jobs",
        ),
        (
            [{"id": 11, "head_sha": "other", "status": "completed", "conclusion": "success"}],
            {"total_count": 1},
            "missing",
        ),
    ],
)
def test_rejects_tests_without_startup_evidence(monkeypatch, runs, jobs, message):
    result, _ = _verify(monkeypatch, runs, jobs)
    assert message in result


@pytest.mark.parametrize("status,conclusion", [("in_progress", None), ("completed", "success")])
def test_accepts_tests_with_jobs_for_the_exact_head(monkeypatch, status, conclusion):
    result, calls = _verify(
        monkeypatch,
        [
            {"id": 11, "head_sha": "head", "status": status, "conclusion": conclusion},
        ],
        {"total_count": 1},
    )
    assert result == ""
    assert calls == [
        "repos/owner/repo/actions/workflows/test.yml/runs?head_sha=head&event=pull_request&per_page=1",
        "repos/owner/repo/actions/runs/11/jobs?filter=latest&per_page=1",
    ]


def test_api_failure_cannot_pass(monkeypatch):
    from scripts import ci_tests_start

    def failed(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(ci_tests_start.subprocess, "run", failed)
    with pytest.raises(subprocess.CalledProcessError):
        ci_tests_start.verify("owner/repo", "head", attempts=1, interval=0)


@pytest.mark.parametrize(
    "problem,code,output",
    [("", 0, "Tests startup verified for this head.\n"), ("missing", 1, "::error::missing\n")],
    ids=["started", "missing"],
)
def test_command_reports_startup_result(monkeypatch, capsys, problem, code, output):
    from scripts import ci_tests_start

    calls = []
    monkeypatch.setattr(ci_tests_start, "verify", lambda *args: calls.append(args) or problem)
    assert ci_tests_start.main(["--repository", "owner/repo", "--head", "head"]) == code
    assert calls == [("owner/repo", "head")]
    assert capsys.readouterr().out == output


def test_waits_for_a_run_and_its_first_job(monkeypatch):
    from scripts import ci_tests_start

    replies = iter(
        [
            {"workflow_runs": []},
            {"workflow_runs": [{"id": 11, "head_sha": "head", "status": "queued", "conclusion": None}]},
            {"total_count": 0},
            {"workflow_runs": [{"id": 11, "head_sha": "head", "status": "in_progress", "conclusion": None}]},
            {"total_count": 1},
        ]
    )
    sleeps = []
    monkeypatch.setattr(
        ci_tests_start.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, json.dumps(next(replies)), ""),
    )
    monkeypatch.setattr(ci_tests_start.time, "sleep", sleeps.append)
    assert ci_tests_start.verify("owner/repo", "head", attempts=3, interval=2) == ""
    assert sleeps == [2, 2]


def test_does_not_sleep_after_final_attempt(monkeypatch):
    from scripts import ci_tests_start

    monkeypatch.setattr(
        ci_tests_start.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, '{"workflow_runs": []}', ""),
    )
    monkeypatch.setattr(ci_tests_start.time, "sleep", lambda _: pytest.fail("slept after final attempt"))
    assert ci_tests_start.verify("owner/repo", "head", attempts=1) == "Tests run is missing for this head."


def test_api_uses_checked_bounded_json_request(monkeypatch):
    from scripts import ci_tests_start

    calls = []

    def request(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, '{"key": "value"}', "")

    monkeypatch.setattr(ci_tests_start.subprocess, "run", request)
    assert ci_tests_start._api("endpoint") == {"key": "value"}
    assert calls == [
        ((["gh", "api", "endpoint"],), {"check": True, "capture_output": True, "text": True, "timeout": 30})
    ]
