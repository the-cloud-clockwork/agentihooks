import json
import subprocess
from datetime import UTC, datetime

import pytest

from scripts import ci_minutes

pytestmark = pytest.mark.unit

TEST = ".github/workflows/test.yml"
PREFLIGHT = ".github/workflows/mutation-preflight.yml"


def run(id, event, branch="feature", path=TEST):
    return {"id": id, "event": event, "head_branch": branch, "path": path}


def job(run_id, name, minutes, labels=("anton-oss",)):
    return {
        "run_id": run_id,
        "name": name,
        "labels": list(labels),
        "started_at": "2026-10-24T10:00:00Z",
        "completed_at": f"2026-10-24T10:{minutes:02d}:00Z",
    }


@pytest.mark.parametrize(
    ("event", "branch", "expected"),
    [
        ("pull_request", "feature", "pull request"),
        ("pull_request_target", "feature", "pull request"),
        ("merge_group", "gh-readonly-queue/dev/pr-1-abc", "queue"),
        ("push", "dev", "promotion"),
        ("push", "feature", "branch push"),
        ("schedule", "dev", "schedule"),
    ],
)
def test_each_event_maps_to_its_tier(event, branch, expected):
    assert ci_minutes.tier(run(1, event, branch)) == expected


def test_matrix_suffixes_fold_into_one_job_name():
    assert ci_minutes.job_name("unit (3.12, 17)") == "unit"
    assert ci_minutes.job_name("mutation (4)") == "mutation"
    assert ci_minutes.job_name("semgrep / scan") == "semgrep / scan"


def test_a_job_that_never_started_costs_nothing():
    assert ci_minutes.seconds({"started_at": None, "completed_at": "2026-10-24T10:00:00Z"}) == 0.0
    assert ci_minutes.seconds({"started_at": "2026-10-24T10:00:00Z", "completed_at": None}) == 0.0
    assert ci_minutes.seconds(job(1, "x", 3)) == 180.0


def test_minutes_split_by_tier_workflow_and_job_per_merged_pull_request():
    runs = [run(1, "pull_request"), run(2, "merge_group"), run(3, "push", "feature", PREFLIGHT)]
    jobs = [
        job(1, "unit (3.12, 1)", 5),
        job(1, "unit (3.12, 2)", 5),
        job(2, "unit (3.12, 1)", 4),
        job(3, "stats (0)", 6),
        job(1, "lint", 9, labels=("ubuntu-latest",)),
        job(99, "orphan", 30),
    ]
    result = ci_minutes.aggregate(runs, jobs, merged=2)
    assert result["anton_minutes"] == 20.0
    assert result["hosted_minutes"] == 9.0
    assert result["anton_per_merged"] == 10.0
    assert result["tiers"] == [
        {"tier": "pull request", "minutes": 10.0, "per_merged": 5.0},
        {"tier": "branch push", "minutes": 6.0, "per_merged": 3.0},
        {"tier": "queue", "minutes": 4.0, "per_merged": 2.0},
    ]
    assert result["workflows"][0] == {"workflow": TEST, "minutes": 14.0, "per_merged": 7.0}
    assert result["jobs_by_name"][0] == {
        "tier": "pull request",
        "workflow": TEST,
        "job": "unit",
        "minutes": 10.0,
        "per_merged": 5.0,
    }


def test_no_merged_pull_request_reports_no_ratio():
    result = ci_minutes.aggregate([run(1, "pull_request")], [job(1, "unit", 5)], merged=0)
    assert result["anton_per_merged"] is None
    assert "per merged pull request None" in ci_minutes.render(result, "a", "b")


def test_runs_are_read_one_hour_at_a_time(monkeypatch):
    paths = []

    def fake(path, jq):
        paths.append(path)
        return [run(len(paths), "push"), run(1, "push")]

    monkeypatch.setattr(ci_minutes, "_gh", fake)
    since = datetime(2026, 10, 24, 0, tzinfo=UTC)
    found = ci_minutes.read_runs("o/r", since, datetime(2026, 10, 24, 3, tzinfo=UTC))
    assert sorted(paths) == [
        "repos/o/r/actions/runs?created=2026-10-24T00:00:00Z..2026-10-24T00:59:59Z&per_page=100",
        "repos/o/r/actions/runs?created=2026-10-24T01:00:00Z..2026-10-24T01:59:59Z&per_page=100",
        "repos/o/r/actions/runs?created=2026-10-24T02:00:00Z..2026-10-24T02:59:59Z&per_page=100",
    ]
    assert sorted(item["id"] for item in found) == [1, 2, 3]


def test_main_writes_the_measurement(monkeypatch, tmp_path):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        path = command[3] if command[2] == "--paginate" else command[2]
        if path.startswith("search/"):
            return subprocess.CompletedProcess(command, 0, "4\n", "")
        if "/jobs?" in path:
            return subprocess.CompletedProcess(command, 0, json.dumps(job(7, "unit (3.12, 1)", 8)) + "\n", "")
        return subprocess.CompletedProcess(command, 0, json.dumps(run(7, "merge_group")) + "\n", "")

    monkeypatch.setattr(ci_minutes.subprocess, "run", fake_run)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    out = tmp_path / "m.json"
    assert ci_minutes.main(["--repo", "o/r", "--until", "2026-10-24T02:00:00Z", "--hours", "2", "--out", str(out)]) == 0
    result = json.loads(out.read_text())
    assert result["merged"] == 4
    assert result["anton_minutes"] == 8.0
    assert result["anton_per_merged"] == 2.0
    assert result["since"] == "2026-10-24T00:00:00Z"
    assert any("merged:2026-10-24T00:00:00Z..2026-10-24T02:00:00Z" in part for command in calls for part in command)
