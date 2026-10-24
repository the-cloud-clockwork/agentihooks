import json
import subprocess
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from scripts.doctor import ci_speed, ci_speed_read, cli
from scripts.inbox.store import InboxStore
from tests.doctor.test_ci_speed_timer import DOCTOR, WATCHED, snapshot, store  # noqa: F401

pytestmark = pytest.mark.unit

REFINE = {
    "completed_at": "2026-10-19T15:34:05Z",
    "conclusion": "success",
    "created_at": "2026-10-19T15:31:56Z",
    "id": 118148964043,
    "labels": ["anton-oss-light"],
    "name": "refine",
    "run_attempt": 1,
    "run_id": 39278021878,
    "runner_group_name": "anton-oss",
    "runner_id": 350958,
    "runner_name": "anton-oss-light-cpwxt-runner-q9qv5",
    "started_at": "2026-10-19T15:32:02Z",
    "status": "completed",
}
STALE = {
    **REFINE,
    "completed_at": None,
    "conclusion": None,
    "runner_id": None,
    "runner_name": None,
    "runner_group_name": None,
    "started_at": "2026-10-19T15:31:56Z",
    "status": "queued",
}
OLD = {**STALE, "created_at": "2026-10-19T15:22:31Z", "started_at": "2026-10-19T15:22:31Z"}
PREFLIGHT = {
    "id": 39278021878,
    "name": "Mutation preflight",
    "status": "in_progress",
    "event": "push",
    "run_attempt": 1,
    "path": ".github/workflows/mutation-preflight.yml",
}
HELD = {
    "created_at": "2026-10-19T16:44:19Z",
    "event": "push",
    "head_branch": "dev",
    "id": 39287956231,
    "name": "Tests",
    "path": ".github/workflows/test.yml",
    "run_attempt": 1,
    "status": "pending",
    "updated_at": "2026-10-19T16:44:33Z",
}
FOUND_AT = int(datetime.fromisoformat("2026-10-19T15:37:32+00:00").timestamp() * 1000)
WAIT_ID = 'ci-runner-wait/39278021878-1-["anton-oss-light"]'
AGE_ID = 'ci-queue-age-unconfirmed/39278021878-1-["anton-oss-light"]'


def actions(runs, jobs, reread):
    calls = []

    def run(args, **kwargs):
        calls.append(args[2])
        path = urlsplit(args[2]).path
        if path.endswith("/jobs"):
            body = {"total_count": len(jobs), "jobs": list(jobs)}
        elif path.endswith("/runs"):
            body = {"total_count": len(runs), "workflow_runs": list(runs)}
        elif "/actions/jobs/" in path:
            body = reread[int(path.rsplit("/", 1)[1])]
            if body is None:
                raise subprocess.CalledProcessError(1, args)
        else:
            body = runs[0]
        return SimpleNamespace(stdout=json.dumps(body))

    return run, calls


def test_a_recorded_stale_queued_snapshot_of_a_job_started_in_seconds_raises_no_runner_wait():
    run, calls = actions([PREFLIGHT], [STALE], {REFINE["id"]: REFINE})
    result = ci_speed_read.read(FOUND_AT - 60000, FOUND_AT, run=run)
    assert result["complete"] is True
    assert ci_speed.findings(result["runs"], FOUND_AT - 60000, FOUND_AT) == []
    assert any(call.endswith(f"/actions/jobs/{REFINE['id']}") for call in calls)


def test_a_wait_the_reread_cannot_confirm_is_queue_age_not_runner_wait():
    run, _ = actions([PREFLIGHT], [OLD], {REFINE["id"]: OLD})
    result = ci_speed_read.read(FOUND_AT - 60000, FOUND_AT, run=run)
    [finding] = ci_speed.findings(result["runs"], FOUND_AT - 60000, FOUND_AT)
    assert finding.id == AGE_ID
    assert finding.measure == 901
    assert finding.threshold == "unconfirmed queue age over 15 minutes"


def test_a_fresh_queued_job_is_not_reread():
    young = {**STALE, "created_at": "2026-10-19T15:37:00Z", "started_at": "2026-10-19T15:37:00Z"}
    run, calls = actions([PREFLIGHT], [young], {})
    ci_speed_read.read(FOUND_AT - 60000, FOUND_AT, run=run)
    assert not any("/actions/jobs/" in call for call in calls)


def test_a_runner_assignment_confirms_the_start_of_a_job_still_marked_queued():
    assigned = {**REFINE, "status": "queued", "completed_at": None}
    assert ci_speed.findings([{**PREFLIGHT, "jobs": [assigned]}], FOUND_AT - 60000, FOUND_AT) == []
    late = {**assigned, "started_at": "2026-10-19T15:37:00Z"}
    [finding] = ci_speed.findings(
        [{**PREFLIGHT, "status": "completed", "conclusion": "success", "jobs": [late]}], FOUND_AT - 60000, FOUND_AT
    )
    assert finding.id == WAIT_ID and finding.measure == 304


def test_a_confirmed_long_wait_remains_a_runner_wait():
    late = {**REFINE, "started_at": "2026-10-19T15:37:00Z"}
    [finding] = ci_speed.findings(
        [{**PREFLIGHT, "status": "completed", "conclusion": "success", "jobs": [late]}], FOUND_AT - 60000, FOUND_AT
    )
    assert finding.id == WAIT_ID
    assert finding.threshold == "runner queue wait over 5 minutes"


def test_a_recorded_concurrency_held_run_lists_no_jobs_and_raises_no_wait():
    run, _ = actions([HELD], [], {})
    now = int(datetime.fromisoformat("2026-10-19T16:50:00+00:00").timestamp() * 1000)
    result = ci_speed_read.read(now - 60000, now, run=run)
    assert result["runs"] == [{**HELD, "jobs": []}]
    assert ci_speed.findings(result["runs"], now - 60000, now) == []


def test_rereads_stop_at_the_pass_cap_and_leave_the_rest_unconfirmed():
    stale = [{**OLD, "id": n} for n in range(ci_speed_read.REREADS + 1)]
    run, calls = actions([PREFLIGHT], stale, {n: OLD for n in range(ci_speed_read.REREADS + 1)})
    result = ci_speed_read.read(FOUND_AT - 60000, FOUND_AT, run=run)
    assert sum("/actions/jobs/" in call for call in calls) == ci_speed_read.REREADS
    assert result["unverified"] == 1
    [finding] = ci_speed.findings(result["runs"], FOUND_AT - 60000, FOUND_AT)
    assert finding.kind == "ci queue age unconfirmed"


def test_a_late_start_supersedes_the_queue_age_but_keeps_the_runner_wait():
    late = {**REFINE, "started_at": "2026-10-19T15:37:00Z"}
    gone = ci_speed.cleared([{**PREFLIGHT, "status": "completed", "conclusion": "success", "jobs": [late]}])
    assert AGE_ID in gone
    assert WAIT_ID not in gone
    assert set(ci_speed.cleared([{**PREFLIGHT, "jobs": [REFINE]}])) >= {AGE_ID, WAIT_ID}
    assert ci_speed.cleared([{**PREFLIGHT, "jobs": [STALE]}]) == []


def test_a_finding_superseded_by_a_corrected_read_is_retracted(store, monkeypatch):  # noqa: F811
    rereads = iter([OLD, REFINE])
    original = ci_speed_read.read

    def read(since, now, pending=None, known=None, active=True):
        run, _ = actions([PREFLIGHT], [OLD], {REFINE["id"]: next(rereads)})
        return original(since, now, pending, known, run=run, active=active)

    monkeypatch.setattr(ci_speed_read, "read", read)
    cli.timer(store, DOCTOR, FOUND_AT, False)
    details = store.redis.hgetall(store.key(DOCTOR, "doctor-details"))
    raised = 'ci-queue-age-unconfirmed/["anton-oss-light"]'
    assert list(details) == [raised]
    cli.timer(store, DOCTOR, FOUND_AT + 60000, False)
    assert raised not in store.redis.hgetall(store.key(DOCTOR, "doctor-details"))
    judged = json.loads(store.redis.hget(store.key(DOCTOR, "doctor-findings"), raised))
    assert judged["retracted_at"] == FOUND_AT + 60000
    for slug in (DOCTOR, WATCHED):
        texts = [item.text for item in InboxStore(store.redis).inbox(f"master@{slug}")]
        assert any(f"supersedes them.\n- {raised}" in text for text in texts)
        assert not store.redis.sismember(store.key(DOCTOR, "ci-speed-notified", slug), raised)


def test_failed_rereads_count_as_unverified_and_the_rest_are_still_corrected():
    jobs = [{**STALE, "id": 1}, {**STALE, "id": 2}, {**STALE, "id": 3}, {**REFINE, "id": 4}]
    run, _ = actions([PREFLIGHT], jobs, {1: None, 2: None, 3: {**REFINE, "id": 3}})
    result = ci_speed_read.read(FOUND_AT - 60000, FOUND_AT, run=run)
    assert result["unverified"] == 2
    [read] = result["runs"]
    assert [job["id"] for job in read["jobs"]] == [1, 2, 3, 4]
    assert read["jobs"][2] == {**REFINE, "id": 3}


def test_a_job_queued_exactly_five_minutes_is_not_reread():
    edge = {**STALE, "created_at": "2026-10-19T15:32:32Z", "started_at": "2026-10-19T15:32:32Z"}
    run, calls = actions([PREFLIGHT], [edge], {})
    assert ci_speed_read.read(FOUND_AT - 60000, FOUND_AT, run=run)["unverified"] == 0
    assert not any("/actions/jobs/" in call for call in calls)


def test_a_start_exactly_five_minutes_after_creation_clears_the_runner_wait():
    edge = {**REFINE, "started_at": "2026-10-19T15:36:56Z"}
    assert WAIT_ID in ci_speed.cleared([{**PREFLIGHT, "jobs": [edge]}])


def test_one_unstarted_job_keeps_every_finding_of_its_group():
    assert ci_speed.cleared([{**PREFLIGHT, "jobs": [STALE, REFINE]}]) == []


def test_the_status_line_reports_queued_jobs_not_reread(store, monkeypatch):  # noqa: F811
    monkeypatch.setattr(ci_speed_read, "read", lambda *args, **kwargs: snapshot(unverified=3))
    actions_taken = cli.timer(store, DOCTOR, FOUND_AT, False)
    assert any("0 retracted, 3 queued jobs not reread" in action for action in actions_taken)


def test_a_confirmed_runner_wait_covers_the_still_queued_siblings_of_its_run_and_labels():
    late = {**REFINE, "id": 1, "started_at": "2026-10-19T15:37:00Z"}
    sibling = {**STALE, "id": 2, "created_at": "2026-10-19T15:21:56Z", "started_at": "2026-10-19T15:21:56Z"}
    found = ci_speed.findings(
        [{**PREFLIGHT, "status": "completed", "conclusion": "success", "jobs": [late, sibling]}],
        FOUND_AT - 60000,
        FOUND_AT,
    )
    assert [finding.id for finding in found] == [WAIT_ID]


def test_an_unconfirmed_queue_age_is_held_until_it_outlasts_the_next_doctor_pass():
    assert ci_speed.findings([{**PREFLIGHT, "jobs": [STALE]}], FOUND_AT - 60000, FOUND_AT) == []
    edge = {**STALE, "created_at": "2026-10-19T15:22:32Z", "started_at": "2026-10-19T15:22:32Z"}
    assert ci_speed.findings([{**PREFLIGHT, "jobs": [edge]}], FOUND_AT - 60000, FOUND_AT) == []
    [finding] = ci_speed.findings([{**PREFLIGHT, "jobs": [OLD]}], FOUND_AT - 60000, FOUND_AT)
    assert finding.id == AGE_ID
    assert finding.measure == 901
    assert finding.threshold == "unconfirmed queue age over 15 minutes"


def test_a_confirmed_wait_retracts_an_earlier_queue_age_of_its_still_queued_siblings():
    late = {**REFINE, "id": 1, "started_at": "2026-10-19T15:37:00Z"}
    sibling = {**OLD, "id": 2}
    assert [finding.id for finding in ci_speed.findings([{**PREFLIGHT, "jobs": [sibling]}], 0, FOUND_AT)] == [AGE_ID]
    gone = ci_speed.cleared([{**PREFLIGHT, "status": "completed", "conclusion": "success", "jobs": [late, sibling]}])
    assert gone == [AGE_ID]
