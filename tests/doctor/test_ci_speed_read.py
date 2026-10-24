import json
import subprocess
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

pytestmark = pytest.mark.unit
NOW = 1792274400000


def api(records=(), jobs=(), fail=False):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if fail:
            raise subprocess.CalledProcessError(1, args, stderr="API unavailable")
        url = urlsplit(args[2])
        query = parse_qs(url.query)
        page = int(query.get("page", [1])[0])
        if url.path.endswith("/jobs"):
            body = {"total_count": len(jobs), "jobs": list(jobs)[(page - 1) * 100 : page * 100]}
        elif url.path.endswith("/runs"):
            selected = list(records) if "created" in query else []
            body = {"total_count": len(selected), "workflow_runs": selected[(page - 1) * 100 : page * 100]}
        else:
            body = {"id": int(url.path.rsplit("/", 1)[1]), "status": "completed", "event": "push"}
        return SimpleNamespace(stdout=json.dumps(body))

    return run, calls


def test_repository_runs_and_all_job_pages_are_discovered_with_full_counts():
    from scripts.doctor import ci_speed_read

    records = [{"id": n, "status": "in_progress", "event": "push"} for n in range(101)]
    run, calls = api(records, [{"id": n} for n in range(101)])
    result = ci_speed_read.read(NOW - 60000, NOW, run=run, budget=(300, 25))
    assert result["complete"] is True
    assert len(result["runs"]) == result["discovered_runs"] == 101
    assert sum(len(r["jobs"]) for r in result["runs"]) == 10201
    assert result["api_calls"] == len(calls) == 209
    assert all(0 < kwargs["timeout"] <= 25 for _, kwargs in calls)


def test_prior_active_runs_are_read_after_their_completion_between_passes():
    from scripts.doctor import ci_speed_read

    run, calls = api(jobs=[{"id": 19}])
    result = ci_speed_read.read(NOW - 60000, NOW, pending=[42], run=run)
    assert result["complete"] is True
    assert result["runs"] == [{"id": 42, "status": "completed", "event": "push", "jobs": [{"id": 19}]}]
    assert any(args[2].endswith("/runs/42") for args, _ in calls)


def test_api_failure_is_explicit_instead_of_a_successful_zero():
    from scripts.doctor import ci_speed_read

    run, calls = api(fail=True)
    result = ci_speed_read.read(NOW - 60000, NOW, run=run)
    assert result["complete"] is False
    assert "API" in result["error"] and result["api_calls"] == len(calls) == 1


def test_request_budget_exhaustion_reports_an_incomplete_pass():
    from scripts.doctor import ci_speed_read

    run, calls = api([{"id": n} for n in range(101)])
    result = ci_speed_read.read(NOW - 60000, NOW, run=run, budget=(1, 25))
    assert result["complete"] is False
    assert "budget" in result["error"] and result["api_calls"] == len(calls) == 1


def test_active_runs_are_discovered_even_when_they_predate_the_window():
    from scripts.doctor import ci_speed_read

    calls = []

    def run(args, **kwargs):
        calls.append(args[2])
        if "status=queued" in args[2]:
            body = {"total_count": 1, "workflow_runs": [{"id": 88, "status": "queued", "event": "push"}]}
        elif "/jobs?" in args[2]:
            body = {"total_count": 1, "jobs": [{"id": 9}]}
        else:
            body = {"total_count": 0, "workflow_runs": []}
        return SimpleNamespace(stdout=json.dumps(body))

    result = ci_speed_read.read(NOW - 60000, NOW, pending=[], run=run)
    assert result["complete"] is True and result["runs"][0]["id"] == 88
    assert len(calls) == 7


@pytest.mark.parametrize("max_seconds", [25, 100])
def test_api_requests_use_the_exact_repository_window_and_safe_subprocess_options(monkeypatch, max_seconds):
    from scripts.doctor import ci_speed_read

    monkeypatch.setattr(ci_speed_read.time, "monotonic", lambda: 100)
    run, calls = api()
    result = ci_speed_read.read(NOW - 60000, NOW, run=run, budget=(60, max_seconds))
    assert result == {"complete": True, "runs": [], "api_calls": 6, "discovered_runs": 0, "unverified": 0}
    first_args, _ = calls[0]
    assert first_args[:2] == ["gh", "api"]
    assert urlsplit(first_args[2]).path == "repos/the-cloud-clockwork/agentihooks/actions/runs"
    assert parse_qs(urlsplit(first_args[2]).query) == {
        "created": ["2026-10-17T21:59:00+00:00..2026-10-17T22:00:00+00:00"],
        "per_page": ["100"],
        "page": ["1"],
    }
    for args, kwargs in calls:
        assert args[:2] == ["gh", "api"]
        assert kwargs == {"capture_output": True, "text": True, "check": True, "timeout": 25}


def test_incomplete_pagination_reports_the_missing_full_count():
    from scripts.doctor import ci_speed_read

    def run(args, **kwargs):
        return SimpleNamespace(stdout=json.dumps({"total_count": 1, "workflow_runs": []}))

    result = ci_speed_read.read(NOW - 60000, NOW, run=run)
    assert result == {
        "complete": False,
        "error": "Actions API read failed: RuntimeError: Actions API pagination ended before the full count: "
        "endpoint=repos/the-cloud-clockwork/agentihooks/actions/runs?"
        "created=2026-10-17T21%3A59%3A00%2B00%3A00..2026-10-17T22%3A00%3A00%2B00%3A00; "
        "page=1; rows=0; total=1",
        "runs": [],
        "api_calls": 2,
        "discovered_runs": 0,
    }


@pytest.mark.parametrize("target", ["runs", "jobs"])
def test_pagination_accepts_growing_totals_and_deduplicates_ids(target):
    from scripts.doctor import ci_speed_read

    calls = []
    fallback, _ = api([{"id": 999}])

    def run(args, **kwargs):
        url = urlsplit(args[2])
        query = parse_qs(url.query)
        selected = (target == "runs" and "created" in query) or (target == "jobs" and url.path.endswith("/jobs"))
        if not selected:
            return fallback(args, **kwargs)
        page = int(query["page"][0])
        calls.append(page)
        rows = [{"id": n} for n in range(100)] if page == 1 else [{"id": 99}, {"id": 100}]
        field = "workflow_runs" if target == "runs" else "jobs"
        return SimpleNamespace(stdout=json.dumps({"total_count": 101 if page == 1 else 103, field: rows}))

    result = ci_speed_read.read(NOW - 60000, NOW, run=run, budget=(300, 25))
    assert result["complete"] is True
    rows = result["runs"] if target == "runs" else result["runs"][0]["jobs"]
    assert sorted(row["id"] for row in rows) == list(range(101))
    assert calls == [1, 2]


@pytest.mark.parametrize("target", ["runs", "jobs"])
def test_a_short_list_is_retried_from_its_first_page(target):
    from scripts.doctor import ci_speed_read

    pages = []
    fallback, _ = api([{"id": 999}])

    def run(args, **kwargs):
        url = urlsplit(args[2])
        query = parse_qs(url.query)
        selected = (target == "runs" and "created" in query) or (target == "jobs" and url.path.endswith("/jobs"))
        if not selected:
            return fallback(args, **kwargs)
        pages.append(int(query["page"][0]))
        field = "workflow_runs" if target == "runs" else "jobs"
        rows = [] if len(pages) == 1 else [{"id": 1}, {"id": 2}]
        return SimpleNamespace(stdout=json.dumps({"total_count": 2, field: rows}))

    result = ci_speed_read.read(NOW - 60000, NOW, run=run)
    assert result["complete"] is True
    rows = result["runs"] if target == "runs" else result["runs"][0]["jobs"]
    assert sorted(row["id"] for row in rows) == [1, 2]
    assert pages == [1, 1]


@pytest.mark.parametrize("target", ["runs", "jobs"])
def test_duplicate_rows_do_not_hide_a_persistently_incomplete_list(target):
    from scripts.doctor import ci_speed_read

    pages = []
    fallback, _ = api([{"id": 999}])

    def run(args, **kwargs):
        url = urlsplit(args[2])
        query = parse_qs(url.query)
        selected = (target == "runs" and "created" in query) or (target == "jobs" and url.path.endswith("/jobs"))
        if not selected:
            return fallback(args, **kwargs)
        page = int(query["page"][0])
        pages.append(page)
        rows = [{"id": n} for n in range(100)] if page == 1 else [{"id": 99}, {"id": 100}]
        field = "workflow_runs" if target == "runs" else "jobs"
        return SimpleNamespace(stdout=json.dumps({"total_count": 102, field: rows}))

    result = ci_speed_read.read(NOW - 60000, NOW, run=run)
    assert result["complete"] is False
    assert pages == [1, 2, 1, 2]
    expected_endpoint = (
        "repos/the-cloud-clockwork/agentihooks/actions/runs?"
        "created=2026-10-17T21%3A59%3A00%2B00%3A00..2026-10-17T22%3A00%3A00%2B00%3A00"
        if target == "runs"
        else "repos/the-cloud-clockwork/agentihooks/actions/runs/999/jobs?filter=latest"
    )
    assert result["error"] == (
        "Actions API read failed: RuntimeError: Actions API pagination ended before the full count: "
        f"endpoint={expected_endpoint}; page=2; rows=101; total=102"
    )


@pytest.mark.parametrize("stale,complete", [(1, True), (2, True), (5, True), (6, False)])
def test_a_status_count_still_holding_a_run_that_left_it_completes_once_the_count_settles(monkeypatch, stale, complete):
    from scripts.doctor import ci_speed_read

    sleeps, reads = [], []
    monkeypatch.setattr(ci_speed_read.time, "sleep", sleeps.append)
    queued = [{"id": n, "status": "queued", "event": "push"} for n in range(1, 7)]
    finished = [{"id": 7, "status": "completed", "event": "pull_request"}]

    def run(args, **kwargs):
        query = parse_qs(urlsplit(args[2]).query)
        if "/jobs" in args[2]:
            body = {"total_count": 0, "jobs": []}
        elif "created" in query:
            body = {"total_count": 7, "workflow_runs": queued + finished}
        elif query.get("status") == ["queued"]:
            reads.append(query["page"])
            body = {"total_count": 7 if len(reads) <= stale else 6, "workflow_runs": queued}
        else:
            body = {"total_count": 0, "workflow_runs": []}
        return SimpleNamespace(stdout=json.dumps(body))

    result = ci_speed_read.read(NOW - 60000, NOW, run=run)
    assert result["complete"] is complete
    assert reads == [["1"]] * min(stale + 1, 6)
    assert sleeps == [1] * min(stale - 1, ci_speed_read.SETTLE_READS)
    if complete:
        assert sorted(r["id"] for r in result["runs"]) == list(range(1, 8))
    else:
        assert result["error"] == (
            "Actions API read failed: RuntimeError: Actions API pagination ended before the full count: "
            "endpoint=repos/the-cloud-clockwork/agentihooks/actions/runs?status=queued; page=1; rows=6; total=7"
        )


@pytest.mark.parametrize("elapsed,settles", [(22.5, 4), (23, 0)])
def test_settling_reads_stop_when_the_time_budget_cannot_cover_another(monkeypatch, elapsed, settles):
    from scripts.doctor import ci_speed_read

    sleeps, times = [], iter([100])
    monkeypatch.setattr(ci_speed_read.time, "sleep", sleeps.append)
    monkeypatch.setattr(ci_speed_read.time, "monotonic", lambda: next(times, 100 + elapsed))

    def run(args, **kwargs):
        stale = parse_qs(urlsplit(args[2]).query).get("status") == ["queued"]
        return SimpleNamespace(stdout=json.dumps({"total_count": int(stale), "workflow_runs": []}))

    result = ci_speed_read.read(NOW - 60000, NOW, run=run)
    assert sleeps == [1] * settles
    assert result["api_calls"] == 3 + settles
    assert result["error"] == (
        "Actions API read failed: RuntimeError: Actions API pagination ended before the full count: "
        "endpoint=repos/the-cloud-clockwork/agentihooks/actions/runs?status=queued; page=1; rows=0; total=1"
    )


def test_a_short_multi_page_status_list_is_not_settled(monkeypatch):
    from scripts.doctor import ci_speed_read

    sleeps, pages = [], []
    monkeypatch.setattr(ci_speed_read.time, "sleep", sleeps.append)

    def run(args, **kwargs):
        query = parse_qs(urlsplit(args[2]).query)
        if query.get("status") != ["queued"]:
            return SimpleNamespace(stdout=json.dumps({"total_count": 0, "workflow_runs": []}))
        page = int(query["page"][0])
        pages.append(page)
        rows = [{"id": n} for n in range(100)] if page == 1 else [{"id": 100}]
        return SimpleNamespace(stdout=json.dumps({"total_count": 102, "workflow_runs": rows}))

    result = ci_speed_read.read(NOW - 60000, NOW, run=run)
    assert pages == [1, 2, 1, 2] and sleeps == []
    assert result["error"] == (
        "Actions API read failed: RuntimeError: Actions API pagination ended before the full count: "
        "endpoint=repos/the-cloud-clockwork/agentihooks/actions/runs?status=queued; page=2; rows=101; total=102"
    )


def test_a_short_single_page_job_list_is_not_settled(monkeypatch):
    from scripts.doctor import ci_speed_read

    sleeps, reads = [], []
    monkeypatch.setattr(ci_speed_read.time, "sleep", sleeps.append)
    fallback, _ = api([{"id": 5, "status": "completed", "event": "push"}])

    def run(args, **kwargs):
        if "/jobs" not in args[2]:
            return fallback(args, **kwargs)
        reads.append(args[2])
        return SimpleNamespace(stdout=json.dumps({"total_count": 2, "jobs": [{"id": 1}]}))

    result = ci_speed_read.read(NOW - 60000, NOW, run=run)
    assert len(reads) == 2 and sleeps == []
    assert result["error"].endswith(
        "endpoint=repos/the-cloud-clockwork/agentihooks/actions/runs/5/jobs?filter=latest; page=1; rows=1; total=2"
    )


@pytest.mark.parametrize("elapsed,complete", [(24.5, True), (25, False), (25.5, False)])
def test_the_default_time_budget_allows_work_until_its_deadline(monkeypatch, elapsed, complete):
    from scripts.doctor import ci_speed_read

    times = iter([100])
    monkeypatch.setattr(ci_speed_read.time, "monotonic", lambda: next(times, 100 + elapsed))
    run, calls = api()
    result = ci_speed_read.read(NOW - 60000, NOW, run=run)
    assert result["complete"] is complete
    if complete:
        assert calls and all(kwargs["timeout"] == 0.5 for _, kwargs in calls)
    else:
        assert calls == []
        assert result["error"] == "Actions API read failed: RuntimeError: Actions API request or time budget exhausted"


def test_one_run_attempt_reads_that_attempt_and_every_job_page():
    from scripts.doctor import ci_speed_read

    run, calls = api(jobs=[{"id": n} for n in range(150)])
    record = ci_speed_read.attempt(42, 2, run=run)
    assert record == {"id": 2, "status": "completed", "event": "push", "jobs": [{"id": n} for n in range(150)]}
    root = "repos/the-cloud-clockwork/agentihooks/actions/runs/42/attempts/2"
    assert [args[2] for args, _ in calls] == [
        root,
        f"{root}/jobs?per_page=100&page=1",
        f"{root}/jobs?per_page=100&page=2",
    ]


def test_one_run_attempt_raises_when_the_api_fails():
    from scripts.doctor import ci_speed_read

    run, _ = api(fail=True)
    with pytest.raises(subprocess.CalledProcessError):
        ci_speed_read.attempt(42, 1, run=run)


@pytest.mark.parametrize("jobs, complete", [(1900, True), (1901, False)])
def test_one_run_attempt_allows_twenty_requests_by_default(jobs, complete):
    from scripts.doctor import ci_speed_read

    run, calls = api(jobs=[{"id": n} for n in range(jobs)])
    if complete:
        assert len(ci_speed_read.attempt(42, 1, run=run)["jobs"]) == jobs
    else:
        with pytest.raises(RuntimeError, match="budget exhausted"):
            ci_speed_read.attempt(42, 1, run=run)
    assert len(calls) == 20


def test_one_run_attempt_allows_sixty_seconds_by_default(monkeypatch):
    from scripts.doctor import ci_speed_read

    clock = iter([0, 59.5])
    monkeypatch.setattr(ci_speed_read.time, "monotonic", lambda: next(clock, 60))
    run, calls = api()
    with pytest.raises(RuntimeError, match="budget exhausted"):
        ci_speed_read.attempt(42, 1, run=run)
    assert [kwargs["timeout"] for _, kwargs in calls] == [0.5]


def test_one_run_attempt_reads_a_short_job_list_twice_without_settling():
    from scripts.doctor import ci_speed_read

    calls = []

    def run(args, **kwargs):
        calls.append(args[2])
        body = {"total_count": 5, "jobs": [{"id": 1}]} if "/jobs" in args[2] else {"id": 1}
        return SimpleNamespace(stdout=json.dumps(body))

    with pytest.raises(RuntimeError, match="pagination ended before the full count"):
        ci_speed_read.attempt(42, 1, run=run)
    assert len(calls) == 3


def test_the_default_request_budget_stops_after_sixty_calls():
    from scripts.doctor import ci_speed_read

    run, calls = api([{"id": n} for n in range(101)])
    result = ci_speed_read.read(NOW - 60000, NOW, run=run)
    assert result["complete"] is False
    assert result["api_calls"] == len(calls) == 60
    assert result["discovered_runs"] == 101
    assert result["error"] == "Actions API read failed: RuntimeError: Actions API request or time budget exhausted"


def test_api_failure_preserves_diagnostic_type_and_counts():
    from scripts.doctor import ci_speed_read

    run, calls = api(fail=True)
    result = ci_speed_read.read(NOW - 60000, NOW, run=run)
    assert result == {
        "complete": False,
        "error": "Actions API read failed: CalledProcessError: Command '"
        + str(calls[0][0])
        + "' returned non-zero exit status 1.",
        "runs": [],
        "api_calls": 1,
        "discovered_runs": 0,
    }


def test_api_job_reads_have_a_fixed_concurrency_ceiling(monkeypatch):
    from unittest.mock import Mock

    from scripts.doctor import ci_speed_read

    pool = Mock(wraps=ci_speed_read.ThreadPoolExecutor)
    monkeypatch.setattr(ci_speed_read, "ThreadPoolExecutor", pool)
    run, _ = api([{"id": n} for n in range(10)])
    result = ci_speed_read.read(NOW - 60000, NOW, run=run)
    assert result["complete"] is True
    pool.assert_called_once_with(max_workers=6)


def test_kept_jobs_are_reused_only_for_runs_still_finished_on_the_same_attempt():
    from scripts.doctor import ci_speed_read

    records = [
        {"id": 1, "status": "completed", "run_attempt": 1, "event": "push"},
        {"id": 2, "status": "completed", "run_attempt": 2, "event": "push"},
        {"id": 3, "status": "in_progress", "run_attempt": 1, "event": "push"},
        {"id": 4, "status": "completed", "run_attempt": 1, "event": "push"},
    ]
    kept = {str(n): {"status": "completed", "run_attempt": 1, "jobs": [{"id": "kept"}]} for n in (1, 2, 3)}
    run, calls = api(records, [{"id": "read"}])
    result = ci_speed_read.read(NOW - 60000, NOW, known=kept, run=run)
    assert result["complete"] is True
    assert {r["id"]: r["jobs"][0]["id"] for r in result["runs"]} == {1: "kept", 2: "read", 3: "read", 4: "read"}
    assert sorted(args[2].split("/")[-2] for args, _ in calls if "/jobs?" in args[2]) == ["2", "3", "4"]


def test_a_failed_job_read_still_keeps_every_other_finished_read():
    from scripts.doctor import ci_speed_read

    run, calls = api([{"id": n, "status": "completed", "event": "push"} for n in range(101)], [{"id": 5}])
    result = ci_speed_read.read(NOW - 60000, NOW, run=run)
    assert result["complete"] is False
    assert result["error"] == "Actions API read failed: RuntimeError: Actions API request or time budget exhausted"
    assert len(result["runs"]) == result["api_calls"] - 7 == 53
