import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import ci_stale_runs

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
QUEUED = ("--paginate", "repos/o/r/actions/runs?status=queued&per_page=100", "--jq", ".workflow_runs[]")
RUNS = "repos/o/r/actions/runs"
SWEEP = 'python -m scripts.ci_stale_runs --repo "$GITHUB_REPOSITORY"'
WATCH = 'python -m scripts.ci_lost_runner --repo "$GITHUB_REPOSITORY"'


def stamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def run(run_id: int, updated: datetime, created: datetime | None = None) -> dict[str, Any]:
    return {
        "id": run_id,
        "name": "Tests",
        "head_branch": f"engineer-{run_id}",
        "created_at": stamp(created or updated),
        "updated_at": stamp(updated),
    }


def job(status: str, *labels: str) -> dict[str, Any]:
    return {"status": status, "labels": list(labels)}


def jobs_call(run_id: int) -> tuple[str, ...]:
    return ("--paginate", f"{RUNS}/{run_id}/jobs?filter=all&per_page=100", "--jq", ".jobs[]")


def lines(*items: dict[str, Any]) -> str:
    return "".join(json.dumps(item) + "\n" for item in items)


class Api:
    def __init__(self, pages: dict[tuple[str, ...], str | list[str]], refused: dict[str, str] | None = None) -> None:
        self.pages = pages
        self.refused = refused or {}
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str) -> str:
        self.calls.append(args)
        if args[0] == "-X":
            if args[2] in self.refused:
                raise subprocess.CalledProcessError(1, ["gh", "api", *args], stderr=self.refused[args[2]])
            return ""
        value = self.pages[args]
        return value.pop(0) if isinstance(value, list) else value

    def posts(self) -> list[str]:
        return [call[2] for call in self.calls if call[0] == "-X"]


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(ci_stale_runs.time, "sleep", slept.append)
    return slept


def test_api_calls_the_rate_limited_gh_with_its_arguments_as_text(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake(args: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, options))
        return subprocess.CompletedProcess(args, 0, stdout="listed")

    monkeypatch.setattr(ci_stale_runs, "gh_api", fake)
    assert ci_stale_runs._api("--paginate", "repos/o/r/actions/runs") == "listed"
    assert calls == [(["--paginate", "repos/o/r/actions/runs"], {"timeout": 120, "text": True})]


def test_queued_reads_every_page_of_queued_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = run(1, NOW), run(2, NOW)
    api = Api({QUEUED: lines(first, second)})
    monkeypatch.setattr(ci_stale_runs, "_api", api)
    assert ci_stale_runs.queued("o/r") == [first, second]
    assert api.calls == [QUEUED]


@pytest.mark.feature("stale-queued-run-sweep")
def test_a_run_untouched_for_a_day_is_stale_and_a_younger_one_is_not() -> None:
    day = run(1, NOW - timedelta(hours=24))
    younger = run(2, NOW - timedelta(hours=24) + timedelta(seconds=1))
    assert ci_stale_runs.stale([day, younger], NOW) == [day]


def test_a_rerun_of_an_old_run_is_not_stale() -> None:
    rerun = run(1, NOW - timedelta(hours=1), created=NOW - timedelta(days=3))
    assert ci_stale_runs.stale([rerun], NOW) == []


def test_jobs_counts_job_states_by_runner_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    pair = job("queued", "self-hosted", "anton-oss")
    found = lines(pair, job("completed", "ubuntu-latest"), pair)
    api = Api({jobs_call(1): found, jobs_call(2): ""})
    monkeypatch.setattr(ci_stale_runs, "_api", api)
    assert ci_stale_runs.jobs("o/r", 1) == "1 completed on ubuntu-latest, 2 queued on self-hosted,anton-oss"
    assert ci_stale_runs.jobs("o/r", 2) == "no jobs"


def test_clear_logs_each_run_then_cancels_and_stops_once_none_is_queued(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float], capsys: pytest.CaptureFixture[str]
) -> None:
    runs = [run(1, NOW - timedelta(days=8)), run(2, NOW - timedelta(days=2))]
    api = Api({jobs_call(1): "", jobs_call(2): lines(job("queued", "anton-oss")), QUEUED: lines(run(9, NOW))})
    monkeypatch.setattr(ci_stale_runs, "_api", api)
    assert ci_stale_runs.clear("o/r", runs) == []
    assert api.posts() == [f"{RUNS}/1/cancel", f"{RUNS}/2/cancel"]
    assert no_sleep == [30]
    assert capsys.readouterr().out.splitlines() == [
        "run 1 Tests on engineer-1, created 2026-11-11T12:00:00Z, updated 2026-11-11T12:00:00Z: no jobs",
        "run 2 Tests on engineer-2, created 2026-11-17T12:00:00Z, updated 2026-11-17T12:00:00Z: 1 queued on anton-oss",
        "0 of 2 stale runs still queued after cancel",
    ]


@pytest.mark.feature("stale-queued-run-sweep")
def test_clear_force_cancels_only_the_runs_a_cancel_left_queued(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float], capsys: pytest.CaptureFixture[str]
) -> None:
    runs = [run(run_id, NOW - timedelta(days=2)) for run_id in (3, 1, 2)]
    fresh = run(9, NOW)
    listings = [lines(runs[2], fresh), lines(fresh)]
    api = Api({jobs_call(1): "", jobs_call(2): "", jobs_call(3): "", QUEUED: listings})
    monkeypatch.setattr(ci_stale_runs, "_api", api)
    assert ci_stale_runs.clear("o/r", runs) == []
    assert api.posts() == [f"{RUNS}/1/cancel", f"{RUNS}/2/cancel", f"{RUNS}/3/cancel", f"{RUNS}/2/force-cancel"]
    assert no_sleep == [30, 30]
    assert capsys.readouterr().out.splitlines()[-2:] == [
        "1 of 3 stale runs still queued after cancel",
        "0 of 3 stale runs still queued after force-cancel",
    ]


def test_clear_returns_the_runs_that_stay_queued_after_force_cancel(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    stuck = run(1, NOW - timedelta(days=2))
    api = Api({jobs_call(1): "", QUEUED: lines(stuck)})
    monkeypatch.setattr(ci_stale_runs, "_api", api)
    assert ci_stale_runs.clear("o/r", [stuck]) == [1]
    assert api.posts() == [f"{RUNS}/1/cancel", f"{RUNS}/1/force-cancel"]
    assert no_sleep == [30, 30]


def test_a_refused_cancel_is_logged_and_the_sweep_goes_on(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float], capsys: pytest.CaptureFixture[str]
) -> None:
    runs = [run(1, NOW - timedelta(days=2)), run(2, NOW - timedelta(days=2))]
    refusal = "HTTP 409: Cannot cancel a workflow run that is completed."
    api = Api({jobs_call(1): "", jobs_call(2): "", QUEUED: ""}, refused={f"{RUNS}/1/cancel": refusal + "\n"})
    monkeypatch.setattr(ci_stale_runs, "_api", api)
    assert ci_stale_runs.clear("o/r", runs) == []
    assert api.posts() == [f"{RUNS}/1/cancel", f"{RUNS}/2/cancel"]
    assert capsys.readouterr().out.splitlines() == [
        "run 1 Tests on engineer-1, created 2026-11-17T12:00:00Z, updated 2026-11-17T12:00:00Z: no jobs",
        "run 2 Tests on engineer-2, created 2026-11-17T12:00:00Z, updated 2026-11-17T12:00:00Z: no jobs",
        f"run 1 refused cancel: {refusal}",
        "0 of 2 stale runs still queued after cancel",
    ]


@pytest.mark.feature("stale-queued-run-sweep")
def test_main_sweeps_only_stale_runs_and_warns_for_each_one_left(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float], capsys: pytest.CaptureFixture[str]
) -> None:
    now = datetime.now(UTC)
    stuck, fresh = run(1, now - timedelta(days=2)), run(2, now - timedelta(minutes=1))
    api = Api({jobs_call(1): "", QUEUED: [lines(stuck, fresh), lines(stuck, fresh), lines(stuck, fresh)]})
    monkeypatch.setattr(ci_stale_runs, "_api", api)
    assert ci_stale_runs.main(["--repo", "o/r"]) == 0
    assert api.posts() == [f"{RUNS}/1/cancel", f"{RUNS}/1/force-cancel"]
    assert capsys.readouterr().out.splitlines()[-1] == "::warning::run 1 stays queued after cancel and force-cancel"


def test_main_makes_no_call_past_the_listing_when_nothing_is_stale(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float], capsys: pytest.CaptureFixture[str]
) -> None:
    api = Api({QUEUED: lines(run(2, datetime.now(UTC) - timedelta(minutes=1)))})
    monkeypatch.setattr(ci_stale_runs, "_api", api)
    assert ci_stale_runs.main(["--repo", "o/r"]) == 0
    assert api.calls == [QUEUED]
    assert no_sleep == []
    assert capsys.readouterr().out == "0 stale queued runs\n"


def test_main_needs_a_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ci_stale_runs, "_api", Api({}))
    with pytest.raises(SystemExit):
        ci_stale_runs.main([])


@pytest.mark.feature("stale-queued-run-sweep")
def test_the_lost_runner_workflow_sweeps_stale_runs_with_its_app_token_before_the_watch() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/lost-runner.yml").read_text())
    steps = workflow["jobs"]["watch"]["steps"]
    runs = [step.get("run") for step in steps]
    mint = next(index for index, step in enumerate(steps) if step.get("id") == "app-token")
    assert steps[mint]["with"]["permission-actions"] == "write"
    assert mint < runs.index(SWEEP) < runs.index(WATCH)
    assert steps[runs.index(SWEEP)]["env"] == {
        "GH_TOKEN": "${{ steps.app-token.outputs.token }}",
        "PYTHONUNBUFFERED": "1",
    }
