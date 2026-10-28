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
