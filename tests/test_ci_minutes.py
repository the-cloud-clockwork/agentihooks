import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from scripts import ci_minutes

pytestmark = pytest.mark.unit

TEST = ".github/workflows/test.yml"
PREFLIGHT = ".github/workflows/mutation-preflight.yml"


def run(id, event, branch="feature", path=TEST):
    return {"id": id, "run_attempt": 1, "event": event, "head_branch": branch, "path": path}


def job(run_id, name, spent, labels=("anton-oss",)):
    return {
        "run_id": run_id,
        "name": name,
        "labels": list(labels) if labels is not None else None,
        "started_at": "2026-10-24T10:00:00Z",
        "completed_at": f"2026-10-24T10:{spent // 60:02d}:{spent % 60:02d}Z",
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


def test_a_job_that_never_started_or_finished_costs_nothing():
    assert ci_minutes.seconds({"started_at": None, "completed_at": "2026-10-24T10:00:00Z"}) == 0.0
    assert ci_minutes.seconds({"started_at": "2026-10-24T10:00:00Z", "completed_at": None}) == 0.0
    assert ci_minutes.seconds({}) == 0.0
    assert ci_minutes.seconds(job(1, "x", 0)) == 0.0
    assert ci_minutes.seconds(job(1, "x", 187)) == 187.0


def test_only_anton_labels_count_as_anton():
    assert ci_minutes.on_anton(job(1, "x", 1, labels=("anton-oss-light",)))
    assert not ci_minutes.on_anton(job(1, "x", 1, labels=("ubuntu-latest",)))
    assert not ci_minutes.on_anton(job(1, "x", 1, labels=None))
    assert not ci_minutes.on_anton(job(1, "x", 1, labels=()))


def test_minutes_split_by_tier_workflow_and_job_per_merged_pull_request():
    runs = [run(1, "pull_request"), run(2, "merge_group"), run(3, "push", "feature", PREFLIGHT)]
    jobs = [
        job(99, "orphan", 1800),
        job(1, "unit (3.12, 1)", 307),
        job(1, "unit (3.12, 2)", 300),
        job(2, "unit (3.12, 1)", 244),
        job(3, "stats (0)", 365),
        job(1, "lint", 545, labels=("ubuntu-latest",)),
        job(1, "gate", 20, labels=None),
    ]
    assert ci_minutes.aggregate(runs, jobs, merged=2) == {
        "runs": 3,
        "jobs": 7,
        "merged": 2,
        "anton_minutes": 20.3,
        "hosted_minutes": 9.4,
        "anton_per_merged": 10.1,
        "tiers": [
            {"tier": "pull request", "minutes": 10.1, "per_merged": 5.1},
            {"tier": "branch push", "minutes": 6.1, "per_merged": 3.0},
            {"tier": "queue", "minutes": 4.1, "per_merged": 2.0},
        ],
        "workflows": [
            {"workflow": TEST, "minutes": 14.2, "per_merged": 7.1},
            {"workflow": PREFLIGHT, "minutes": 6.1, "per_merged": 3.0},
        ],
        "jobs_by_name": [
            {"tier": "pull request", "workflow": TEST, "job": "unit", "minutes": 10.1, "per_merged": 5.1},
            {"tier": "branch push", "workflow": PREFLIGHT, "job": "stats", "minutes": 6.1, "per_merged": 3.0},
            {"tier": "queue", "workflow": TEST, "job": "unit", "minutes": 4.1, "per_merged": 2.0},
        ],
    }


def test_no_merged_pull_request_reports_no_ratio():
    result = ci_minutes.aggregate([run(1, "pull_request")], [job(1, "unit", 300)], merged=0)
    assert result["anton_per_merged"] is None
    assert result["tiers"] == [{"tier": "pull request", "minutes": 5.0, "per_merged": None}]


def measured(names=1):
    return {
        "since": "S",
        "until": "U",
        "runs": 3,
        "jobs": 7,
        "merged": 2,
        "anton_minutes": 20.3,
        "hosted_minutes": 9.4,
        "anton_per_merged": 10.1,
        "tiers": [{"tier": "queue", "minutes": 4.1, "per_merged": 2.0}],
        "workflows": [{"workflow": TEST, "minutes": 14.2, "per_merged": 7.1}],
        "jobs_by_name": [
            {"tier": "queue", "workflow": TEST, "job": f"j{index}", "minutes": 1.0, "per_merged": 0.5}
            for index in range(names)
        ],
    }


def test_the_report_renders_every_table():
    assert ci_minutes.render(measured()) == "\n".join(
        [
            "## Anton runner minutes S to U",
            "",
            "3 runs, 7 jobs, 2 pull requests merged into dev.",
            "Anton minutes 20.3, per merged pull request 10.1; hosted minutes 9.4.",
            "",
            "| Tier | Minutes | Per merged |",
            "| --- | ---: | ---: |",
            "| queue | 4.1 | 2.0 |",
            "",
            "| Workflow | Minutes | Per merged |",
            "| --- | ---: | ---: |",
            f"| {TEST} | 14.2 | 7.1 |",
            "",
            "| Tier | Workflow | Job | Minutes | Per merged |",
            "| --- | --- | --- | ---: | ---: |",
            f"| queue | {TEST} | j0 | 1.0 | 0.5 |",
            "",
        ]
    )


def test_the_job_table_keeps_the_top_twenty_five():
    lines = ci_minutes.render(measured(26)).splitlines()
    assert f"| queue | {TEST} | j24 | 1.0 | 0.5 |" in lines
    assert f"| queue | {TEST} | j25 | 1.0 | 0.5 |" not in lines


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci-minutes.yml"


def listed(id, status="completed", created="2026-10-24T10:00:00Z"):
    return {**run(id, "pull_request"), "status": status, "created_at": created}


def workflow_steps():
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]["measure"]["steps"]


def test_the_runner_minutes_token_comes_from_an_app_the_swarm_does_not_spend():
    token = next(step for step in workflow_steps() if step.get("id") == "app-token")
    assert token["with"]["client-id"] == "${{ secrets.TCC_CI_CLIENT_ID }}"
    assert token["with"]["private-key"] == "${{ secrets.TCC_CI_APP_PRIVATE_KEY }}"
    assert "AGENTICORE" not in WORKFLOW.read_text()


def test_the_workflow_carries_the_jobs_cache_between_dispatches():
    steps = workflow_steps()
    restore = next(step for step in steps if step.get("uses", "").startswith("actions/cache/restore"))
    save = next(step for step in steps if step.get("uses", "").startswith("actions/cache/save"))
    assert restore["with"]["path"] == save["with"]["path"] == ci_minutes.CACHE.name
    assert save["if"] == "${{ always() }}"
    measure = next(step for step in steps if "python -m scripts.ci_minutes" in step.get("run", ""))
    assert f"--cache {ci_minutes.CACHE.name}" in measure["run"]


def test_completed_runs_already_cached_are_never_read_again(monkeypatch):
    read = []

    def rows(path, key):
        assert key == "jobs"
        read.append(path)
        return [job(int(path.split("/")[-2]), "unit", 60)]

    monkeypatch.setattr(ci_minutes, "rows", rows)
    cache = {"1:1": {"created_at": "2026-10-24T10:00:00Z", "jobs": [job(1, "cached", 120)]}}
    runs = [listed(1), listed(2), listed(3, status="in_progress")]
    jobs = ci_minutes.read_jobs("o/r", runs, cache)
    assert sorted(read) == [
        "repos/o/r/actions/runs/2/jobs?filter=all&per_page=100",
        "repos/o/r/actions/runs/3/jobs?filter=all&per_page=100",
    ]
    assert sorted(job["name"] for job in jobs) == ["cached", "unit", "unit"]
    assert sorted(cache) == ["1:1", "2:1"]
    assert cache["2:1"] == {"created_at": "2026-10-24T10:00:00Z", "jobs": [job(2, "unit", 60)]}


def test_a_rerun_attempt_of_a_cached_run_is_read_again(monkeypatch):
    read = []
    monkeypatch.setattr(ci_minutes, "rows", lambda path, key: read.append(path) or [job(1, "rerun", 60)])
    cache = {"1:1": {"created_at": "2026-10-24T10:00:00Z", "jobs": [job(1, "first", 120)]}}
    jobs = ci_minutes.read_jobs("o/r", [{**listed(1), "run_attempt": 2}], cache)
    assert read == ["repos/o/r/actions/runs/1/jobs?filter=all&per_page=100"]
    assert jobs == [job(1, "rerun", 60)]
    assert sorted(cache) == ["1:1", "1:2"]


def test_a_second_pass_over_the_same_completed_window_reads_no_jobs(monkeypatch):
    calls = []
    monkeypatch.setattr(ci_minutes, "rows", lambda path, key: calls.append(path) or [job(7, "unit", 60)])
    cache = {}
    first = ci_minutes.read_jobs("o/r", [listed(7)], cache)
    second = ci_minutes.read_jobs("o/r", [listed(7)], cache)
    assert len(calls) == 1
    assert first == second == [job(7, "unit", 60)]


def test_the_cache_round_trips_and_drops_runs_older_than_the_kept_span(tmp_path):
    path = tmp_path / "cache.json"
    since = datetime(2026, 10, 24, tzinfo=UTC)
    kept = since - ci_minutes.KEEP
    cache = {
        "1:1": {"created_at": "2026-10-23T23:00:00Z", "jobs": [job(1, "unit", 60)]},
        "2:1": {"created_at": kept.strftime(ci_minutes.STAMP), "jobs": []},
        "3:1": {"created_at": "2026-10-10T00:00:00Z", "jobs": []},
    }
    ci_minutes.save_cache(path, cache, since)
    assert ci_minutes.load_cache(path) == {"1:1": cache["1:1"], "2:1": cache["2:1"]}
    assert ci_minutes.load_cache(tmp_path / "missing.json") == {}


def test_a_read_that_would_leave_less_than_the_reserve_is_refused(monkeypatch):
    monkeypatch.setattr(ci_minutes, "remaining", lambda: ci_minutes.RESERVE + 10)
    ci_minutes.within_budget(10)
    with pytest.raises(SystemExit, match="11 job reads"):
        ci_minutes.within_budget(11)


def test_remaining_reads_the_core_limit_from_the_free_rate_limit_endpoint(monkeypatch):
    def fake(args, **kwargs):
        assert args == ["gh", "api", "rate_limit"]
        assert kwargs["check"] is True
        return subprocess.CompletedProcess(args, 0, json.dumps({"resources": {"core": {"remaining": 4321}}}), "")

    monkeypatch.setattr(ci_minutes.subprocess, "run", fake)
    assert ci_minutes.remaining() == 4321


def test_main_reads_only_uncached_runs_and_keeps_the_cache(monkeypatch, tmp_path):
    cache = tmp_path / "jobs.json"
    cache.write_text(json.dumps({"1:1": {"created_at": "2026-10-24T10:00:00Z", "jobs": [job(1, "unit", 60)]}}))
    monkeypatch.setattr(ci_minutes, "read_runs", lambda repo, since, until: [listed(1), listed(2)])
    monkeypatch.setattr(ci_minutes, "merged_count", lambda repo, since, until: 1)
    budgets = []
    monkeypatch.setattr(ci_minutes, "within_budget", budgets.append)
    monkeypatch.setattr(ci_minutes, "rows", lambda path, key: [job(2, "unit", 120)])
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    out = tmp_path / "out.json"
    argv = ["--repo", "o/r", "--until", "2026-10-24T12:00:00Z", "--out", str(out), "--cache", str(cache)]
    assert ci_minutes.main(argv) == 0
    assert budgets == [1]
    assert json.loads(out.read_text())["anton_minutes"] == 3.0
    assert sorted(json.loads(cache.read_text())) == ["1:1", "2:1"]


def test_runs_are_read_one_hour_at_a_time_and_kept_once(monkeypatch):
    calls = []

    def fake(path, key):
        calls.append((path, key))
        return [run(len(calls), "push"), run(1, "push")]

    monkeypatch.setattr(ci_minutes, "rows", fake)
    since = datetime(2026, 10, 24, 0, tzinfo=UTC)
    until = datetime(2026, 10, 24, 2, 30, tzinfo=UTC)
    found = ci_minutes.read_runs("o/r", since, until)
    assert sorted(calls) == [
        ("repos/o/r/actions/runs?created=2026-10-24T00:00:00Z..2026-10-24T00:59:59Z&per_page=100", "workflow_runs"),
        ("repos/o/r/actions/runs?created=2026-10-24T01:00:00Z..2026-10-24T01:59:59Z&per_page=100", "workflow_runs"),
        ("repos/o/r/actions/runs?created=2026-10-24T02:00:00Z..2026-10-24T02:29:59Z&per_page=100", "workflow_runs"),
    ]
    assert sorted(item["id"] for item in found) == [1, 2, 3]
    calls.clear()
    ci_minutes.read_runs("o/r", since, datetime(2026, 10, 24, 2, tzinfo=UTC))
    assert len(calls) == 2


def test_an_hour_at_the_listing_cap_fails_instead_of_undercounting(monkeypatch):
    since = datetime(2026, 10, 24, 0, tzinfo=UTC)
    until = datetime(2026, 10, 24, 1, tzinfo=UTC)
    monkeypatch.setattr(ci_minutes, "rows", lambda path, key: [run(n, "push") for n in range(999)])
    assert len(ci_minutes.read_runs("o/r", since, until)) == 999
    monkeypatch.setattr(ci_minutes, "rows", lambda path, key: [run(n, "push") for n in range(1000)])
    with pytest.raises(
        RuntimeError, match="^2026-10-24T00:00:00Z..2026-10-24T00:59:59Z listed 1000 runs, the most GitHub returns$"
    ):
        ci_minutes.read_runs("o/r", since, until)


def test_jobs_are_read_for_every_attempt_of_every_run(monkeypatch):
    calls = []

    def fake(path, key):
        calls.append((path, key))
        return [{"path": path}]

    monkeypatch.setattr(ci_minutes, "rows", fake)
    found = ci_minutes.read_jobs("o/r", [run(5, "push"), run(6, "push")], {})
    assert found == [
        {"path": "repos/o/r/actions/runs/5/jobs?filter=all&per_page=100"},
        {"path": "repos/o/r/actions/runs/6/jobs?filter=all&per_page=100"},
    ]
    assert {key for _, key in calls} == {"jobs"}


def test_merged_pull_requests_are_counted_in_the_window(monkeypatch):
    calls = []

    def fake(path, key):
        calls.append((path, key))
        return [{}, {}, {}]

    monkeypatch.setattr(ci_minutes, "rows", fake)
    since = datetime(2026, 10, 23, 2, tzinfo=UTC)
    assert ci_minutes.merged_count("o/r", since, datetime(2026, 10, 24, 2, tzinfo=UTC)) == 3
    assert calls == [
        (
            "search/issues?q=repo:o/r+is:pr+is:merged+base:dev"
            "+merged:2026-10-23T02:00:00Z..2026-10-24T02:00:00Z&per_page=100",
            "items",
        )
    ]


def fake_rows(path, key):
    assert "o/r" in path
    if key == "items":
        return [{}] * 4
    if key == "jobs":
        return [job(7, "unit (3.12, 1)", 480)]
    return [run(7, "merge_group")]


def test_main_writes_the_measurement_prints_it_and_appends_the_summary(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ci_minutes, "rows", fake_rows)
    monkeypatch.setattr(ci_minutes, "within_budget", lambda reads: None)
    summary = tmp_path / "summary.md"
    summary.write_text("before\n")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    out = tmp_path / "m.json"
    cache = tmp_path / "jobs.json"
    argv = ["--repo", "o/r", "--until", "2026-10-24T02:00:00Z", "--hours", "2", "--out", str(out)]
    argv += ["--cache", str(cache)]
    assert ci_minutes.main(argv) == 0
    result = json.loads(out.read_text())
    assert out.read_text() == json.dumps(result, indent=2)
    assert result["merged"] == 4
    assert result["anton_minutes"] == 8.0
    assert result["anton_per_merged"] == 2.0
    assert (result["since"], result["until"]) == ("2026-10-24T00:00:00Z", "2026-10-24T02:00:00Z")
    report = ci_minutes.render(result)
    assert capsys.readouterr().out == report + "\n"
    assert summary.read_text() == "before\n" + report


def test_main_defaults_to_the_last_day_into_the_working_folder(monkeypatch, tmp_path):
    seen = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            seen.append(tz)
            return datetime(2026, 10, 24, 12, 0, 0, 500, tzinfo=UTC)

    monkeypatch.setattr(ci_minutes, "rows", fake_rows)
    monkeypatch.setattr(ci_minutes, "datetime", Clock)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.chdir(tmp_path)
    assert ci_minutes.main(["--repo", "o/r"]) == 0
    result = json.loads((tmp_path / "ci-minutes.json").read_text())
    assert (result["since"], result["until"]) == ("2026-10-23T12:00:00Z", "2026-10-24T12:00:00Z")
    assert seen == [UTC]


def test_main_needs_a_repository():
    with pytest.raises(SystemExit):
        ci_minutes.main([])
