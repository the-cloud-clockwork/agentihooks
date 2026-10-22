from datetime import datetime, timezone

import pytest

from scripts.doctor import ci_speed

pytestmark = pytest.mark.unit
NOW = 1792274400000


def stamp(seconds):
    return datetime.fromtimestamp((NOW + seconds * 1000) / 1000, timezone.utc).isoformat()


def job(wait, *, queued=False, created=-1000, name="unit", identity=11):
    return {
        "id": identity,
        "name": name,
        "created_at": stamp(created),
        "started_at": None if queued else stamp(created + wait),
        "completed_at": None,
        "status": "queued" if queued else "in_progress",
        "labels": ["self-hosted", "anton-oss"],
    }


def record(jobs, event="push", identity=7, **extra):
    return {
        "id": identity,
        "event": event,
        "path": ".github/workflows/test.yml",
        "status": "in_progress",
        "jobs": jobs,
        **extra,
    }


def finished(jobs, event="push", identity=7, **extra):
    return record(jobs, event, identity, **({"status": "completed", "conclusion": "success"} | extra))


def test_waiting_shards_share_one_finding_with_full_group_statistics():
    jobs = [job(wait, identity=n) for n, wait in enumerate([301, 310, 320, 330, 340, 350, 360, 400])]
    [finding] = ci_speed.findings([finished(jobs)], NOW - 2000000, NOW)
    assert finding.measure == 400
    assert "8 jobs" in finding.summary
    assert "longest 400 seconds" in finding.evidence[0]
    assert "median 335.0 seconds" in finding.evidence[0]
    assert "anton-oss" in finding.evidence[0]


def test_wait_groups_keep_runs_attempts_and_label_sets_separate():
    first = job(301, identity=11)
    second = job(302, identity=12)
    second["labels"] = ["anton-batch"]
    records = [
        finished([first, second]),
        finished([job(303)], identity=8),
        finished([job(304)], run_attempt=2),
    ]
    found = ci_speed.findings(records, NOW - 2000000, NOW)
    assert len({finding.id for finding in found}) == 4
    assert sorted(finding.measure for finding in found) == [301, 302, 303, 304]


def test_new_wait_keeps_older_group_members_in_its_statistics():
    older = job(400, created=-1000)
    new = job(302, created=-310, identity=12)
    [finding] = ci_speed.findings([finished([older, new])], NOW - 60000, NOW)
    assert "2 jobs" in finding.summary
    assert finding.measure == 400
    assert "median 351.0 seconds" in finding.evidence[0]


def test_a_short_wait_does_not_hide_a_later_long_wait():
    [finding] = ci_speed.findings([finished([job(300), job(301, identity=12)])], NOW - 2000000, NOW)
    assert finding.measure == 301
    assert "1 jobs" in finding.summary


def test_a_finished_run_counts_all_label_groups_regardless_of_start_window():
    old = job(400)
    current = job(302, created=-310, identity=12)
    current["labels"] = ["anton-batch"]
    found = ci_speed.findings([finished([old, current])], NOW - 60000, NOW)
    assert [(finding.measure, "anton-batch" in finding.summary) for finding in found] == [(400, False), (302, True)]
    assert all("1 jobs" in finding.summary for finding in found)


@pytest.mark.parametrize("wait, count", [(299, 0), (300, 0), (301, 1)])
def test_runner_wait_crosses_only_above_five_minutes(wait, count):
    found = ci_speed.findings([finished([job(wait)])], NOW - 2000000, NOW)
    assert len(found) == count
    if count:
        [finding] = found
        assert finding.kind == "ci runner wait"
        assert finding.measure == 301
        assert all(text in " ".join(finding.evidence) for text in ["anton-oss", "1 jobs", "7", "301"])


def test_a_still_queued_job_uses_now_and_remains_visible_after_its_creation_window():
    [finding] = ci_speed.findings([finished([job(0, queued=True, created=-901)])], NOW - 60000, NOW)
    assert finding.kind == "ci queue age unconfirmed" and finding.measure == 901


@pytest.mark.parametrize("status", ["queued", "pending", "waiting"])
def test_unstarted_jobs_ignore_placeholder_start_timestamps(status):
    waiting = job(0, queued=True, created=-901)
    waiting.update(status=status, started_at=waiting["created_at"])
    [finding] = ci_speed.findings([finished([waiting])], NOW - 60000, NOW)
    assert finding.measure == 901


def test_a_finished_run_counts_waits_that_started_before_the_pass_window():
    [finding] = ci_speed.findings([finished([job(301)])], NOW - 60000, NOW)
    assert finding.kind == "ci runner wait" and finding.measure == 301


@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
@pytest.mark.parametrize("elapsed, count", [(899, 0), (900, 0), (901, 1)])
def test_tier_wall_includes_runner_wait_and_crosses_only_above_fifteen_minutes(event, elapsed, count):
    first = job(0, created=-elapsed)
    gate = job(0, created=-10, name="Gate — Required", identity=12)
    gate.update(status="completed", completed_at=stamp(0))
    run = finished([first, gate], event, status="completed", conclusion="success")
    found = ci_speed.findings([run], NOW - 2000000, NOW)
    assert len(found) == count
    if count:
        assert found[0].kind == "ci tier wall time" and found[0].measure == 901
        assert event in " ".join(found[0].evidence)


def test_completed_pr_wall_stops_at_gate_result_and_promotion_has_no_budget():
    first = job(0, created=-1200)
    gate = job(0, created=-400, name="Gate — Required", identity=12)
    gate.update(status="completed", completed_at=stamp(-300))
    assert ci_speed.findings([finished([first, gate], "pull_request")], NOW - 2000000, NOW) == []
    assert ci_speed.findings([finished([first], "push")], NOW - 2000000, NOW) == []


@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
def test_an_unfinished_tier_has_no_wall_time(event):
    run = record([job(0, created=-901)], event)
    assert ci_speed.findings([run], NOW - 2000000, NOW) == []


@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
@pytest.mark.parametrize("status", ["queued", "in_progress"])
@pytest.mark.parametrize("conclusion", ["success", "failure", "cancelled"])
def test_run_timing_is_judged_only_after_completion(event, status, conclusion):
    first = job(307, created=-1200)
    run = finished([first], event, status=status)
    for seconds in [0, 60, 409]:
        found = ci_speed.findings([run], NOW - 60000, NOW + seconds * 1000)
        assert not [entry for entry in found if entry.kind in {"ci runner wait", "ci tier wall time"}]
    first.update(status="completed", completed_at=stamp(409))
    run.update(status="completed", conclusion=conclusion)
    found = ci_speed.findings([run], NOW + 408000, NOW + 409000)
    if conclusion == "cancelled":
        assert found == []
    else:
        assert [(entry.kind, entry.measure) for entry in found] == [
            ("ci runner wait", 307),
            ("ci tier wall time", 1609),
        ]
        assert ci_speed.findings([run], NOW + 409000, NOW + 900000) == found


def test_every_crossing_is_reported_without_a_display_limit():
    records = [finished([job(301)], identity=n) for n in range(150)]
    assert len(ci_speed.findings(records, NOW - 2000000, NOW)) == 150


@pytest.mark.parametrize("wait,count", [(300, 0), (300.001, 1)])
def test_runner_wait_uses_the_exact_millisecond_boundary(wait, count):
    assert len(ci_speed.findings([finished([job(wait)])], NOW - 2000000, NOW)) == count


@pytest.mark.parametrize("elapsed,count", [(900, 0), (900.001, 1)])
def test_tier_wall_uses_the_exact_millisecond_boundary(elapsed, count):
    first = job(0, created=-elapsed)
    first.update(status="completed", completed_at=stamp(0))
    found = ci_speed.findings([finished([first], "merge_group")], NOW - 2000000, NOW)
    assert len(found) == count
    if count:
        assert found[0].measure == 900
        assert found[0].evidence == ("run 7, tier merge_group, conclusion success, wall time 900 seconds",)


@pytest.mark.parametrize("attempt", [None, 3])
@pytest.mark.parametrize("labels,text", [([], "unassigned"), (["self-hosted", "anton-oss"], "anton-oss, self-hosted")])
def test_runner_notice_preserves_labels_attempt_and_human_readable_minutes(attempt, labels, text):
    queued = job(301)
    queued["labels"] = labels
    extra = {} if attempt is None else {"run_attempt": attempt}
    [finding] = ci_speed.findings([finished([queued], **extra)], NOW - 2000000, NOW)
    suffix = "[]" if not labels else '["anton-oss", "self-hosted"]'
    assert finding.subject == f"7-{attempt or 1}-{suffix}"
    assert finding.summary == f"{text} runners waited up to 5.02 minutes for 1 jobs"
    assert finding.evidence == (f"run 7, labels {text}, 1 jobs, longest 301 seconds, median 301.0 seconds",)
    assert finding.threshold == "runner queue wait over 5 minutes"


@pytest.mark.parametrize("attempt,subject", [(None, "7-1"), (3, "7-3")])
def test_tier_notice_preserves_attempt_and_human_readable_minutes(attempt, subject):
    extra = {} if attempt is None else {"run_attempt": attempt}
    first = job(0, created=-901)
    first.update(status="completed", completed_at=stamp(0))
    [finding] = ci_speed.findings([finished([first], "merge_group", **extra)], NOW - 2000000, NOW)
    assert finding.subject == subject
    assert finding.summary == "queue tier took 15.02 minutes"
    assert finding.threshold == "tier wall time over 15 minutes"


def test_a_job_starting_at_the_window_boundary_is_included():
    [finding] = ci_speed.findings([finished([job(301, created=-601)])], NOW - 300000, NOW)
    assert finding.measure == 301


def test_a_queued_job_remains_visible_when_the_cursor_is_after_now():
    [finding] = ci_speed.findings([finished([job(0, queued=True, created=-901)])], NOW + 1, NOW)
    assert finding.measure == 901


def test_unrelated_pull_request_workflows_have_no_tier_budget():
    run = finished([job(0, created=-901)], "pull_request", path=".github/workflows/docs.yml")
    assert ci_speed.findings([run], NOW - 2000000, NOW) == []
    gate = job(0, name="Gate — Required", created=-901)
    gate.update(status="completed", completed_at=stamp(0))
    [finding] = ci_speed.findings([finished([gate], "pull_request", path="renamed.yml")], NOW - 2000000, NOW)
    assert finding.measure == 901


@pytest.mark.parametrize("elapsed,count", [(900, 0), (901, 1)])
def test_a_completed_run_without_a_gate_stops_at_the_last_job(elapsed, count):
    first = job(0, created=-1200)
    first.update(status="completed", completed_at=stamp(-600))
    last = job(0, created=-1100, identity=12)
    last.update(status="completed", completed_at=stamp(elapsed - 1200))
    run = finished([last, job(0, created=-1000), first], "merge_group", status="completed", conclusion="failure")
    found = ci_speed.findings([run], NOW - 2000000, NOW)
    assert len(found) == count
    if count:
        assert found[0].measure == 901


@pytest.mark.parametrize("conclusion", ["success", "failure"])
def test_a_completed_run_without_any_completion_has_no_invented_wall_time(conclusion):
    run = finished([job(0, created=-901)], "merge_group", status="completed", conclusion=conclusion)
    assert ci_speed.findings([run], NOW - 2000000, NOW) == []


def test_a_completed_gate_stops_wall_time_before_later_jobs_finish():
    first = job(0, created=-1200)
    first.update(status="completed", completed_at=stamp(0))
    gate = job(0, created=-400, name="Gate — Required", identity=12)
    gate.update(status="completed", completed_at=stamp(-300))
    run = finished([first, gate], "pull_request", status="completed", conclusion="success")
    assert ci_speed.findings([run], NOW - 2000000, NOW) == []


@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
@pytest.mark.parametrize(
    "conclusion,count",
    [
        ("success", 1),
        ("failure", 1),
        ("cancelled", 0),
        ("skipped", 0),
        ("neutral", 0),
        ("timed_out", 0),
        ("startup_failure", 0),
        ("action_required", 0),
        ("stale", 0),
        (None, 0),
    ],
)
def test_a_finished_tier_counts_only_success_or_failure_and_names_its_conclusion(event, conclusion, count):
    first = job(0, created=-918)
    first.update(status="completed", completed_at=stamp(0))
    run = finished([first], event, status="completed", conclusion=conclusion)
    found = ci_speed.findings([run], NOW - 2000000, NOW)
    assert len(found) == count
    if count:
        assert found[0].evidence == (f"run 7, tier {event}, conclusion {conclusion}, wall time 918 seconds",)


def test_a_superseded_pull_request_run_cancelled_mid_gate_raises_no_tier_wall_time():
    shards = []
    for identity in range(22):
        shard = job(0, created=-918, identity=identity)
        shard.update(status="completed", completed_at=stamp(0))
        shards.append(shard)
    gate = job(0, created=-918, name="Gate — Required", identity=99)
    gate.update(status="completed", completed_at=stamp(0))
    run = finished(shards + [gate], "pull_request", status="completed", conclusion="cancelled")
    assert ci_speed.findings([run], NOW - 2000000, NOW) == []
