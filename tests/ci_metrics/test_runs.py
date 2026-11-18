import io
import json
import subprocess
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from scripts import ci_minutes
from scripts.ci_metrics import runs

pytestmark = pytest.mark.unit

REPO = "the-cloud-clockwork/agentihooks"
SINCE = datetime(2026, 11, 4, 13, tzinfo=UTC)
UNTIL = datetime(2026, 11, 4, 14, tzinfo=UTC)
PULLS = f"repos/{REPO}/pulls?state=closed&base=dev&sort=updated&direction=desc&per_page=100&page="


def pull(number: int, updated: str, merged: str | None) -> dict[str, Any]:
    return {
        "number": number,
        "head": {"ref": f"branch-{number}"},
        "created_at": "2026-11-04T12:00:00Z",
        "updated_at": updated,
        "merged_at": merged,
    }


def test_the_window_is_the_hour_that_ends_at_its_end() -> None:
    assert runs.window(UNTIL) == (SINCE, UNTIL)


def test_the_lag_steps_back_whole_hours_from_the_hour_now_falls_in() -> None:
    now = datetime(2026, 11, 4, 15, 37, 12, 5, tzinfo=UTC)
    assert runs.lagged(now, timedelta(hours=2)) == datetime(2026, 11, 4, 13, tzinfo=UTC)


def test_an_api_read_runs_gh_api_and_parses_its_json(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout='[{"number": 1}]', stderr="")

    monkeypatch.setattr(runs.subprocess, "run", fake)
    assert runs.api("repos/x/pulls") == [{"number": 1}]
    assert calls == [
        (["gh", "api", "repos/x/pulls"], {"capture_output": True, "text": True, "check": True, "timeout": 60})
    ]


def test_merged_pulls_page_until_an_update_older_than_the_window_and_keep_merges_inside_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = {
        1: [
            pull(1, "2026-11-04T15:00:00Z", "2026-11-04T14:00:00Z"),
            pull(2, "2026-11-04T14:30:00Z", "2026-11-04T13:59:59Z"),
            pull(3, "2026-11-04T13:00:00Z", None),
        ],
        2: [
            pull(4, "2026-11-04T13:10:00Z", "2026-11-04T13:00:00Z"),
            pull(5, "2026-11-04T12:59:59Z", "2026-11-04T12:59:59Z"),
        ],
        3: [pull(6, "2026-11-04T12:00:00Z", "2026-11-04T13:30:00Z")],
    }
    paths: list[str] = []

    def fake(path: str) -> list[dict[str, Any]]:
        paths.append(path)
        return pages[int(path.removeprefix(PULLS))]

    monkeypatch.setattr(runs, "api", fake)
    assert runs.merged_pulls(REPO, SINCE, UNTIL) == [
        {"number": 2, "branch": "branch-2", "created_at": "2026-11-04T12:00:00Z", "merged_at": "2026-11-04T13:59:59Z"},
        {"number": 4, "branch": "branch-4", "created_at": "2026-11-04T12:00:00Z", "merged_at": "2026-11-04T13:00:00Z"},
    ]
    assert paths == [f"{PULLS}1", f"{PULLS}2"]


def test_merged_pulls_stop_at_an_empty_page(monkeypatch: pytest.MonkeyPatch) -> None:
    paths: list[str] = []
    monkeypatch.setattr(runs, "api", lambda path: paths.append(path) or [])
    assert runs.merged_pulls(REPO, SINCE, UNTIL) == []
    assert paths == [f"{PULLS}1"]


def test_merged_pulls_refuse_when_ten_pages_do_not_reach_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    paths: list[str] = []
    recent = [pull(1, "2026-11-04T15:00:00Z", None)]
    monkeypatch.setattr(runs, "api", lambda path: paths.append(path) or recent)
    with pytest.raises(SystemExit) as refused:
        runs.merged_pulls(REPO, SINCE, UNTIL)
    assert refused.value.code == (
        "ci metrics refused: 10 pages of closed pull requests did not reach 2026-11-04T13:00:00Z"
    )
    assert paths == [f"{PULLS}{page}" for page in range(1, 11)]


def test_merged_pulls_accept_a_window_the_tenth_page_reaches(monkeypatch: pytest.MonkeyPatch) -> None:
    paths: list[str] = []

    def fake(path: str) -> list[dict[str, Any]]:
        paths.append(path)
        updated = "2026-11-04T12:00:00Z" if path.endswith("page=10") else "2026-11-04T15:00:00Z"
        return [pull(len(paths), updated, None)]

    monkeypatch.setattr(runs, "api", fake)
    assert runs.merged_pulls(REPO, SINCE, UNTIL) == []
    assert len(paths) == 10


def test_branch_runs_list_every_run_on_the_quoted_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(runs, "rows", lambda path, key: calls.append((path, key)) or [{"id": 1}])
    assert runs.branch_runs(REPO, "a/b c") == [{"id": 1}]
    assert calls == [(f"repos/{REPO}/actions/runs?branch=a/b%20c&per_page=100", "workflow_runs")]


def test_pending_reads_the_hourly_average_per_runner_pool_from_prometheus(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, int]] = []
    answer = {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": {"name": "anton-oss"}, "value": [1793800800, "8.94"]},
                {"metric": {"name": "anton-oss-batch"}, "value": [1793800800, "0"]},
            ],
        },
    }

    def fake(url: str, timeout: int) -> io.BytesIO:
        calls.append((url, timeout))
        return io.BytesIO(json.dumps(answer).encode())

    monkeypatch.setattr(runs.urllib.request, "urlopen", fake)
    assert runs.pending("http://prometheus:9090", UNTIL) == {"anton-oss": 8.94, "anton-oss-batch": 0.0}
    query = runs.urllib.parse.urlencode({"query": runs.PENDING, "time": 1793800800.0})
    assert calls == [(f"http://prometheus:9090/api/v1/query?{query}", 60)]
    assert runs.PENDING == "avg_over_time(sum by (name) (gha_controller_pending_ephemeral_runners)[1h:1m])"


def test_read_budgets_the_job_reads_before_it_reads_them(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[Any] = []
    listed = [{"id": 1}, {"id": 2}]
    merged = [{"number": 7, "branch": "feature"}]
    monkeypatch.setattr(
        ci_minutes, "read_runs", lambda repo, since, until: order.append(("runs", repo, since, until)) or listed
    )
    monkeypatch.setattr(
        runs, "merged_pulls", lambda repo, since, until: order.append(("merged", repo, since, until)) or merged
    )
    monkeypatch.setattr(ci_minutes, "within_budget", lambda reads: order.append(("budget", reads)))

    def jobs(repo: str, found: list[dict[str, Any]], cache: dict[str, Any]) -> list[dict[str, Any]]:
        order.append(("jobs", repo, found, cache))
        return [{"id": 10, "run_id": 1}]

    monkeypatch.setattr(ci_minutes, "read_jobs", jobs)
    monkeypatch.setattr(runs, "branch_runs", lambda repo, branch: order.append(("branch", repo, branch)) or [{"id": 3}])
    found = runs.read(REPO, SINCE, UNTIL)
    assert found == runs.Pull(listed, [{"id": 10, "run_id": 1}], merged, [{"id": 3}])
    assert order == [
        ("runs", REPO, SINCE, UNTIL),
        ("merged", REPO, SINCE, UNTIL),
        ("budget", 3),
        ("jobs", REPO, listed, {}),
        ("branch", REPO, "feature"),
    ]
