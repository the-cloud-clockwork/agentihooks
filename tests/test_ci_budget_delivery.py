from types import SimpleNamespace

import fakeredis
import pytest

from scripts import ci_budget

pytestmark = pytest.mark.unit


def _run(run_id, event, sha, **changes):
    return {
        "id": run_id,
        "event": event,
        "head_sha": sha,
        "created_at": "2026-10-09T07:00:00Z",
        "run_started_at": "2026-10-09T07:00:00Z",
        "run_attempt": 1,
        **changes,
    }


def _job(end, **changes):
    return {
        "name": ci_budget.GATE,
        "created_at": "2026-10-09T07:00:00Z",
        "completed_at": end,
        "conclusion": "success",
        **changes,
    }


def _evidence(head=True, queue=True):
    pull = {
        "number": 42,
        "head": {"sha": "final"},
        "merge_commit_sha": "merged",
        "merged_at": "2026-10-09T07:28:00Z",
        "updated_at": "2026-10-09T07:28:00Z",
    }
    data = {
        "repos/owner/repo/pulls/42": [pull],
        "repos/owner/repo/actions/workflows/test.yml/runs?event=pull_request&head_sha=final&per_page=100": [
            _run(10, "pull_request", "final"),
            _run(11, "push", "final"),
            _run(12, "pull_request", "old"),
        ]
        if head
        else [],
        "repos/owner/repo/actions/workflows/test.yml/runs?event=merge_group&head_sha=merged&per_page=100": [
            _run(20, "merge_group", "merged", created_at="2026-10-09T07:20:00Z", run_started_at="2026-10-09T07:21:00Z")
        ]
        if queue
        else [],
        "repos/owner/repo/actions/runs/10/attempts/1/jobs?per_page=100": [_job("2026-10-09T07:08:00Z")],
        "repos/owner/repo/actions/runs/20/attempts/1/jobs?per_page=100": [
            _job("2026-10-09T07:27:00Z", created_at="2026-10-09T07:26:00Z")
        ],
    }
    calls = []

    def read(endpoint, field, **options):
        calls.append((endpoint, field, options))
        return data[endpoint]

    return pull, data, read, calls


def test_final_head_and_queue_are_bound_to_the_merged_pull_request():
    from scripts.ci_budget import delivery

    pull, _, read, calls = _evidence()
    assert delivery.collect("owner/repo", pull, read) == {"head": 480, "queue": 420, "combined": 900, "remaining": 0}
    assert any("head_sha=final" in endpoint for endpoint, _, _ in calls)
    assert any("head_sha=merged" in endpoint for endpoint, _, _ in calls)


@pytest.mark.parametrize(
    "head,queue,expected",
    [
        (False, True, {"head": None, "queue": 420, "combined": None, "remaining": None}),
        (True, False, {"head": 480, "queue": None, "combined": None, "remaining": None}),
        (False, False, {"head": None, "queue": None, "combined": None, "remaining": None}),
    ],
)
def test_missing_runs_stay_unknown(head, queue, expected):
    from scripts.ci_budget import delivery

    pull, _, read, _ = _evidence(head, queue)
    assert delivery.collect("owner/repo", pull, read) == expected


def test_final_attempt_counts_its_own_runner_wait_and_excludes_earlier_attempts():
    from scripts.ci_budget import delivery

    pull, data, read, _ = _evidence()
    path = "repos/owner/repo/actions/workflows/test.yml/runs?event=pull_request&head_sha=final&per_page=100"
    data[path] = [
        _run(30, "pull_request", "final"),
        _run(10, "pull_request", "final", run_attempt=3, run_started_at="2026-10-09T07:02:00Z"),
    ]
    data["repos/owner/repo/actions/runs/10/attempts/3/jobs?per_page=100"] = [
        _job("2026-10-09T07:08:00Z", created_at="2026-10-09T07:02:00Z")
    ]
    assert delivery.collect("owner/repo", pull, read) == {"head": 360, "queue": 420, "combined": 780, "remaining": 120}


def test_budget_comes_from_code_and_negative_remaining_is_preserved(monkeypatch):
    from scripts.ci_budget import delivery

    monkeypatch.setattr(ci_budget, "RUN_BUDGET_S", 800)
    pull, _, read, _ = _evidence()
    result = delivery.collect("owner/repo", pull, read)
    assert result["remaining"] == -100
    assert (
        delivery.render(result)
        == "Delivery budget: head checks 480 seconds; queue checks 420 seconds; combined 900 seconds; remaining minus 100 seconds of 800 seconds."
    )


def test_tick_refresh_records_one_report_on_the_merged_task():
    from scripts.ci_budget import delivery

    _, _, read, _ = _evidence()
    task = {"id": "t1", "state": "done", "pr_url": "https://github.com/owner/repo/pull/42", "comments": []}
    doc = {"tasks": [task]}
    writes = []

    def comment(slug, task_id, text, by):
        writes.append((slug, task_id, text, by))
        task["comments"].append({"text": text})
        return True

    ledger = SimpleNamespace(delivery_budget=comment)
    store = SimpleNamespace(redis=fakeredis.FakeRedis(), key=lambda slug, suffix: f"{slug}:{suffix}")
    config = SimpleNamespace(repo="unused")
    now = int(ci_budget.seconds("2026-10-09T07:30:00Z") * 1000)
    assert delivery.refresh("crew", config, store, ledger, doc, now, read=read) == [
        "recorded merged task delivery budget"
    ]
    assert writes == [
        (
            "crew",
            "t1",
            "Delivery budget: head checks 480 seconds; queue checks 420 seconds; combined 900 seconds; remaining 0 seconds of 900 seconds.",
            "swarm",
        )
    ]
    assert delivery.refresh("crew", config, store, ledger, doc, now + 60_000, read=read) == []
    assert len(writes) == 1


def test_unknown_render_never_claims_time_is_zero():
    from scripts.ci_budget import delivery

    assert (
        delivery.render({"head": None, "queue": None, "combined": None, "remaining": None})
        == "Delivery budget: head checks unknown; queue checks unknown; combined unknown; remaining unknown of 900 seconds."
    )


def test_api_reads_every_page_as_json_without_slurp(monkeypatch):
    import json

    from scripts.ci_budget import delivery

    calls = []

    def run(command, **options):
        calls.append((command, options))
        return SimpleNamespace(stdout=json.dumps({"id": 10}) + "\n\n" + json.dumps({"id": 20}) + "\n")

    monkeypatch.setattr(delivery.subprocess, "run", run)
    assert delivery.api("checkout", "endpoint", ".jobs[]") == [{"id": 10}, {"id": 20}]
    assert calls == [
        (
            ["gh", "api", "endpoint", "--jq", ".jobs[] | @json", "--paginate"],
            {"cwd": "checkout", "check": True, "capture_output": True, "text": True, "timeout": 60},
        )
    ]
    calls.clear()
    assert delivery.api("checkout", "endpoint", ".[]", paginate=False) == [{"id": 10}, {"id": 20}]
    assert calls[0][0] == ["gh", "api", "endpoint", "--jq", ".[] | @json"]


@pytest.mark.parametrize(
    "jobs", [[], [_job(None)], [_job("2026-10-09T07:08:00Z", conclusion="skipped")], [_job("2026-10-09T07:29:00Z")]]
)
def test_missing_completed_required_check_is_unknown(jobs):
    from scripts.ci_budget import delivery

    pull, data, read, _ = _evidence()
    data["repos/owner/repo/actions/runs/10/attempts/1/jobs?per_page=100"] = jobs
    assert delivery.collect("owner/repo", pull, read)["head"] is None


def test_late_task_registration_and_refused_comment_are_retried():
    from scripts.ci_budget import delivery

    pull, _, read, _ = _evidence()
    task = {"id": "t1", "state": "done", "pr_url": "https://github.com/owner/repo/pull/42", "comments": []}
    doc = {"tasks": []}
    calls = []
    accepted = [False, True]

    def save(slug, task_id, text):
        calls.append(text)
        return accepted.pop(0)

    store = SimpleNamespace(redis=fakeredis.FakeRedis(), key=lambda slug, suffix: f"{slug}:{suffix}")
    config = SimpleNamespace(repo="unused")
    ledger = SimpleNamespace(delivery_budget=save)
    now = int(ci_budget.seconds("2026-10-09T07:30:00Z") * 1000)
    assert delivery.refresh("crew", config, store, ledger, doc, now, read=read) == []
    doc["tasks"].append(task)
    assert delivery.refresh("crew", config, store, ledger, doc, now + 3_600_000, read=read) == []
    assert len(calls) == 1
    assert delivery.refresh("crew", config, store, ledger, doc, now + 3_660_000, read=read) == [
        "recorded merged task delivery budget"
    ]
    assert len(calls) == 2
    assert delivery.refresh("crew", config, store, ledger, doc, now + 3_720_000, read=read) == []
    assert len(calls) == 2
