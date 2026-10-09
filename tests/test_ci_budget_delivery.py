import json
from pathlib import Path

import pytest
import yaml

from scripts import ci_budget
from scripts.ci_budget.__main__ import main

pytestmark = pytest.mark.unit

START = "2026-10-09T07:00:00Z"
QUEUE = {
    "event": "merge_group",
    "head_branch": "gh-readonly-queue/dev/pr-42-abcdef",
    "workflow_id": 123,
    "created_at": "2026-10-09T07:10:00Z",
    "run_started_at": "2026-10-09T07:11:00Z",
}


def _head(run_id=10, **changes):
    return {
        "id": run_id,
        "event": "pull_request",
        "head_sha": "final-head",
        "status": "completed",
        "conclusion": "success",
        "created_at": START,
        "run_started_at": START,
        "run_attempt": 2,
        **changes,
    }


def _gate(**changes):
    return {
        "name": ci_budget.GATE,
        "created_at": START,
        "conclusion": "success",
        "completed_at": "2026-10-09T07:08:00Z",
        **changes,
    }


def _api(runs, jobs=None):
    calls = []

    def read(endpoint):
        calls.append(endpoint)
        if endpoint == "repos/owner/repo/pulls/42":
            return [{"head": {"sha": "final-head"}}]
        if endpoint.endswith("runs?event=pull_request&head_sha=final-head&per_page=100"):
            return [{"workflow_runs": page} for page in runs]
        if "/attempts/" in endpoint:
            return [{"jobs": page} for page in (jobs if jobs is not None else [[_gate()]])]
        pytest.fail(endpoint)

    return read, calls


def test_final_head_uses_latest_completed_success_on_exact_head_and_workflow():
    api, calls = _api(
        [
            [_head(30, head_sha="old-head"), _head(29, conclusion="failure")],
            [_head(28, status="in_progress"), _head(20), _head(10)],
        ],
        jobs=[[], [_gate()]],
    )
    assert ci_budget.delivery_head(QUEUE, "owner/repo", api) == {"run": 20, "seconds": 480}
    assert calls == [
        "repos/owner/repo/pulls/42",
        "repos/owner/repo/actions/workflows/123/runs?event=pull_request&head_sha=final-head&per_page=100",
        "repos/owner/repo/actions/runs/20/attempts/2/jobs?per_page=100",
    ]


def test_latest_rerun_is_selected_and_head_runner_wait_is_counted():
    api, calls = _api(
        [
            [
                _head(30, run_started_at="2026-10-09T07:01:00Z"),
                _head(10, run_started_at="2026-10-09T07:02:00Z", run_attempt=3),
                _head(40, event="push"),
            ]
        ],
        jobs=[[_gate(created_at="2026-10-09T07:02:00Z")]],
    )
    queue = {**QUEUE, "head_branch": f"refs/heads/{QUEUE['head_branch']}"}
    assert ci_budget.delivery_head(queue, "owner/repo", api) == {"run": 10, "seconds": 360}
    assert calls[-1] == "repos/owner/repo/actions/runs/10/attempts/3/jobs?per_page=100"


def test_delivery_api_reads_all_pages_without_mutation(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return type("Result", (), {"stdout": '[{"jobs": []}, {"jobs": [{"id": 20}]}]'})()

    monkeypatch.setattr(ci_budget.subprocess, "run", run)
    assert ci_budget.delivery_api("repos/owner/repo/actions/runs/10/jobs?per_page=100") == [
        {"jobs": []},
        {"jobs": [{"id": 20}]},
    ]
    assert calls == [
        (
            ["gh", "api", "--paginate", "--slurp", "repos/owner/repo/actions/runs/10/jobs?per_page=100"],
            {"check": True, "capture_output": True, "text": True},
        )
    ]


@pytest.mark.parametrize(
    "runs,jobs",
    [
        ([[]], None),
        ([[_head()]], [[]]),
        ([[_head()]], [[_gate(conclusion="skipped")]]),
        ([[_head()]], [[_gate(completed_at=None)]]),
        ([[_head()]], [[_gate(completed_at="2026-10-09T07:12:00Z")]]),
    ],
)
def test_missing_final_head_evidence_is_unknown(runs, jobs):
    api, _ = _api(runs, jobs)
    assert ci_budget.delivery_head(QUEUE, "owner/repo", api) is None


def test_unrecognized_queue_ref_is_unknown_without_guessing_a_pull_request():
    assert ci_budget.delivery_head({**QUEUE, "head_branch": "other"}, "owner/repo", None) is None


@pytest.mark.parametrize("event", ["pull_request", "push", "workflow_dispatch"])
def test_other_events_never_read_head_evidence(event):
    assert ci_budget.delivery_head({**QUEUE, "event": event}, "owner/repo", None) is None


def test_delivery_budget_adds_final_head_and_queue_including_runner_wait():
    end = ci_budget.seconds("2026-10-09T07:17:00Z")
    assert ci_budget.delivery_report(QUEUE, {"run": 20, "seconds": 480}, end) == {
        "head": 480,
        "queue": 420,
        "combined": 900,
        "remaining": 0,
    }


def test_delivery_budget_comes_from_code_and_keeps_negative_remaining(monkeypatch):
    monkeypatch.setattr(ci_budget, "RUN_BUDGET_S", 800)
    end = ci_budget.seconds("2026-10-09T07:17:00Z")
    result = ci_budget.delivery_report(QUEUE, {"run": 20, "seconds": 480}, end)
    assert result == {"head": 480, "queue": 420, "combined": 900, "remaining": -100}
    assert ci_budget.delivery_rows(result) == [
        ("final head checks", "8m00s"),
        ("queue checks so far", "7m00s"),
        ("combined delivery", "15m00s"),
        ("remaining delivery", "-1m40s"),
    ]


def test_unknown_head_never_becomes_zero_or_a_known_remaining_budget():
    end = ci_budget.seconds("2026-10-09T07:17:00Z")
    result = ci_budget.delivery_report(QUEUE, None, end)
    assert result == {"head": None, "queue": 420, "combined": None, "remaining": None}
    assert ci_budget.delivery_rows(result) == [
        ("final head checks", "unknown"),
        ("queue checks so far", "7m00s"),
        ("combined delivery", "unknown"),
        ("remaining delivery", "unknown"),
    ]


def test_queue_final_report_uses_gate_completion_and_excludes_earlier_attempts():
    jobs = [
        {"name": "lint", "created_at": "2026-10-09T07:20:00Z", "completed_at": "2026-10-09T07:22:00Z"},
        _gate(created_at="2026-10-09T07:26:00Z", completed_at="2026-10-09T07:27:00Z"),
    ]
    queue = {**QUEUE, "run_attempt": 2}
    end = ci_budget.seconds("2026-10-09T07:30:00Z")
    result = ci_budget.delivery_report(queue, {"seconds": 480}, end, jobs)
    assert result == {"head": 480, "queue": 420, "combined": 900, "remaining": 0}
    assert ci_budget.delivery_rows(result, final=True)[1] == ("queue checks", "7m00s")


def test_provisional_queue_cli_does_not_fetch_head_without_token(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ci_budget, "delivery_api", lambda endpoint: pytest.fail(endpoint))
    run_file, jobs_file = tmp_path / "run.json", tmp_path / "jobs.jsonl"
    run_file.write_text(json.dumps(QUEUE))
    jobs_file.write_text(
        json.dumps(
            {
                "name": "lint",
                "created_at": QUEUE["created_at"],
                "completed_at": "2026-10-09T07:12:00Z",
                "conclusion": "success",
            }
        )
    )
    end = ci_budget.seconds("2026-10-09T07:17:00Z")
    assert main(["--run", str(run_file), "--jobs", str(jobs_file)], now=lambda: end) == 0
    output = capsys.readouterr().out
    assert "final head checks: unknown" in output
    assert "queue checks so far: 7m00s" in output
    assert "combined delivery: unknown" in output
    assert "remaining delivery: unknown" in output


def test_queue_cli_reads_head_through_api_and_reports_to_log_and_summary(tmp_path, monkeypatch, capsys):
    api, _ = _api([[_head()]])
    monkeypatch.setattr(ci_budget, "delivery_api", api)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    run_file, jobs_file = tmp_path / "run.json", tmp_path / "jobs.jsonl"
    run_file.write_text(json.dumps(QUEUE))
    jobs = [
        {
            "name": "lint",
            "created_at": QUEUE["created_at"],
            "completed_at": "2026-10-09T07:12:00Z",
            "conclusion": "success",
        },
        _gate(created_at="2026-10-09T07:17:00Z", completed_at="2026-10-09T07:18:01Z"),
    ]
    jobs_file.write_text("\n".join(json.dumps(job) for job in jobs))
    end = ci_budget.seconds("2026-10-09T07:19:10Z")
    assert main(["--run", str(run_file), "--jobs", str(jobs_file)], now=lambda: end) == 0
    output = capsys.readouterr().out
    for label, duration in [
        ("final head checks", "8m00s"),
        ("queue checks", "8m01s"),
        ("combined delivery", "16m01s"),
        ("remaining delivery", "-1m01s"),
    ]:
        assert f"{label}: {duration}" in output
        assert f"| {label} | {duration} |" in summary.read_text()
    assert "::warning title=Delivery over fifteen minutes::" in output
    assert "15m00s" in summary.read_text()
    assert "Measured through Gate Required." in summary.read_text()


def test_every_queue_report_can_read_pull_request_metadata():
    workflow = yaml.safe_load((Path(__file__).resolve().parents[1] / ".github/workflows/test.yml").read_text())
    job = workflow["jobs"]["delivery-budget"]
    assert job["needs"] == ["gate-required"]
    assert job["if"] == "${{ always() && github.event_name == 'merge_group' }}"
    assert "delivery-budget" not in workflow["jobs"]["gate-required"]["needs"]
    app = next(step for step in job["steps"] if step.get("id") == "app-token")
    assert app["with"]["permission-pull-requests"] == "read"
    reporter = next(step for step in job["steps"] if "scripts.ci_budget" in step.get("run", ""))
    assert reporter["env"]["GH_TOKEN"] == "${{ steps.app-token.outputs.token }}"
