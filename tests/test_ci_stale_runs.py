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

    def requests(self) -> list[tuple[str, ...]]:
        return [call[1:] for call in self.calls if call[0] == "-X"]


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


def test_jobs_lists_every_job_of_a_run(monkeypatch: pytest.MonkeyPatch) -> None:
    found = [job("queued", "anton-oss"), job("completed", "ubuntu-latest")]
    api = Api({jobs_call(1): lines(*found), jobs_call(2): ""})
    monkeypatch.setattr(ci_stale_runs, "_api", api)
    assert ci_stale_runs.jobs("o/r", 1) == found
    assert ci_stale_runs.jobs("o/r", 2) == []


def test_states_counts_job_states_by_runner_labels() -> None:
    pair = job("queued", "self-hosted", "anton-oss")
    summary = ci_stale_runs.states([pair, job("completed", "ubuntu-latest"), pair])
    assert summary == "1 completed on ubuntu-latest, 2 queued on self-hosted,anton-oss"
    assert ci_stale_runs.states([]) == "no jobs"


def test_clear_deletes_each_run_without_jobs_and_cancels_the_rest(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float], capsys: pytest.CaptureFixture[str]
) -> None:
    runs = [run(1, NOW - timedelta(days=8)), run(2, NOW - timedelta(days=2))]
    api = Api({jobs_call(1): "", jobs_call(2): lines(job("queued", "anton-oss")), QUEUED: lines(run(9, NOW))})
    monkeypatch.setattr(ci_stale_runs, "_api", api)
    assert ci_stale_runs.clear("o/r", runs) == []
    assert api.requests() == [("DELETE", f"{RUNS}/1"), ("POST", f"{RUNS}/2/cancel")]
    assert no_sleep == [30]
    assert capsys.readouterr().out.splitlines() == [
        "run 1 Tests on engineer-1, created 2026-11-11T12:00:00Z, updated 2026-11-11T12:00:00Z: no jobs",
        "run 2 Tests on engineer-2, created 2026-11-17T12:00:00Z, updated 2026-11-17T12:00:00Z: 1 queued on anton-oss",
        "0 of 2 stale runs still queued",
    ]


@pytest.mark.feature("stale-queued-run-sweep")
def test_clear_returns_the_stale_runs_still_queued_once_github_settles(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float], capsys: pytest.CaptureFixture[str]
) -> None:
    runs = [run(run_id, NOW - timedelta(days=2)) for run_id in (3, 1, 2)]
    api = Api({jobs_call(1): "", jobs_call(2): "", jobs_call(3): "", QUEUED: lines(runs[2], run(9, NOW))})
    monkeypatch.setattr(ci_stale_runs, "_api", api)
    assert ci_stale_runs.clear("o/r", runs) == [2]
    assert api.requests() == [("DELETE", f"{RUNS}/3"), ("DELETE", f"{RUNS}/1"), ("DELETE", f"{RUNS}/2")]
    assert no_sleep == [30]
    assert capsys.readouterr().out.splitlines()[-1] == "1 of 3 stale runs still queued"


def test_a_refused_request_is_logged_and_the_sweep_goes_on(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float], capsys: pytest.CaptureFixture[str]
) -> None:
    runs = [run(run_id, NOW - timedelta(days=2)) for run_id in (1, 2, 3)]
    forbidden = "gh: Could not delete the workflow run. (HTTP 403)"
    conflict = "gh: Cannot cancel a workflow run that has not been queued yet. (HTTP 409)"
    refused = {f"{RUNS}/1": forbidden + "\n", f"{RUNS}/2/cancel": conflict + "\n"}
    queued_job = lines(job("queued", "anton-oss"))
    api = Api({jobs_call(1): "", jobs_call(2): queued_job, jobs_call(3): "", QUEUED: lines(runs[0], runs[1])}, refused)
    monkeypatch.setattr(ci_stale_runs, "_api", api)
    assert ci_stale_runs.clear("o/r", runs) == [1, 2]
    assert api.requests() == [("DELETE", f"{RUNS}/1"), ("POST", f"{RUNS}/2/cancel"), ("DELETE", f"{RUNS}/3")]
    assert capsys.readouterr().out.splitlines() == [
        "run 1 Tests on engineer-1, created 2026-11-17T12:00:00Z, updated 2026-11-17T12:00:00Z: no jobs",
        f"run 1 refused delete: {forbidden}",
        "run 2 Tests on engineer-2, created 2026-11-17T12:00:00Z, updated 2026-11-17T12:00:00Z: 1 queued on anton-oss",
        f"run 2 refused cancel: {conflict}",
        "run 3 Tests on engineer-3, created 2026-11-17T12:00:00Z, updated 2026-11-17T12:00:00Z: no jobs",
        "2 of 3 stale runs still queued",
    ]


@pytest.mark.feature("stale-queued-run-sweep")
def test_main_sweeps_only_stale_runs_and_warns_for_each_one_left(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float], capsys: pytest.CaptureFixture[str]
) -> None:
    now = datetime.now(UTC)
    stuck, fresh = run(1, now - timedelta(days=2)), run(2, now - timedelta(minutes=1))
    api = Api({jobs_call(1): "", QUEUED: lines(stuck, fresh)})
    monkeypatch.setattr(ci_stale_runs, "_api", api)
    assert ci_stale_runs.main(["--repo", "o/r"]) == 0
    assert api.requests() == [("DELETE", f"{RUNS}/1")]
    assert capsys.readouterr().out.splitlines()[-1] == "::warning::run 1 stays queued after the stale run sweep"


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
    assert "if" not in steps[runs.index(SWEEP)]
    assert steps[runs.index(WATCH)]["if"] == "${{ !cancelled() && steps.app-token.outcome == 'success' }}"
