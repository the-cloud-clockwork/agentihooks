import time
from datetime import datetime, timezone

import pytest

from scripts.doctor import ci_measure
from scripts.doctor import cli as doctor
from scripts.swarm import cli as swarm_cli
from scripts.swarm.store import SwarmError
from tests.doctor.test_doctor_cli import WATCHED, env  # noqa: F401

pytestmark = pytest.mark.unit
NOW = 1792274400000
HOUR_MS = 3600000


def stamp(seconds):
    return datetime.fromtimestamp((NOW + seconds * 1000) / 1000, timezone.utc).isoformat()


def job(created, started, labels=("anton-oss",), name="unit", completed=None):
    return {
        "id": created,
        "name": name,
        "status": "completed" if completed is not None else "in_progress" if started is not None else "queued",
        "labels": list(labels),
        "created_at": stamp(created),
        "started_at": None if started is None else stamp(started),
        "completed_at": None if completed is None else stamp(completed),
        "runner_name": None if started is None else "runner",
    }


def run(identity, jobs, event="pull_request", attempt=1):
    return {
        "id": identity,
        "run_attempt": attempt,
        "event": event,
        "path": ".github/workflows/test.yml",
        "status": "completed",
        "conclusion": "success",
        "jobs": jobs,
    }


def wall(identity, seconds, event="pull_request"):
    return run(identity, [job(-4000, -3990, name="Gate — Required", completed=-4000 + seconds)], event)


WAITED = run(
    20,
    [
        job(-5000, -4560, ("anton-oss-light",)),
        job(-5000, -4900, ("anton-oss-light",)),
        job(-5000, -4990, ("anton-batch",)),
    ],
)
WINDOW = [wall(10, 952), wall(11, 400), wall(12, 1200), wall(13, 2000, "merge_group"), WAITED]


@pytest.fixture
def actions(env, monkeypatch):  # noqa: F811
    assert doctor.main([WATCHED, "start"]) == 0
    monkeypatch.setattr(swarm_cli, "now_ms", lambda: NOW)
    calls = []

    def read(since, now, *args, **kwargs):
        calls.append((since, now))
        return {"complete": True, "runs": WINDOW, "api_calls": 1, "discovered_runs": len(WINDOW)}

    def attempt(identity, number, *args, **kwargs):
        calls.append((identity, number))
        return next(r for r in WINDOW if r["id"] == identity and r["run_attempt"] == number)

    monkeypatch.setattr("scripts.doctor.ci_speed_read.read", read)
    monkeypatch.setattr("scripts.doctor.ci_speed_read.attempt", attempt)
    monkeypatch.setattr(doctor.detect, "readers", lambda *args, **kwargs: {})
    return calls


def lines(capsys):
    return capsys.readouterr().out.splitlines()


def test_wall_time_finding_prints_its_run_value_and_its_tier_over_the_window(actions, capsys):
    capsys.readouterr()
    assert doctor.main([WATCHED, "measure", "ci-tier-wall-time/10-1"]) == 0
    assert lines(capsys) == [
        "ci-tier-wall-time/10-1 952",
        "ci-tier-wall-time pull request, window 2026-10-17T21:00:00Z to 2026-10-17T22:00:00Z: "
        "2 of 3 runs over 900 seconds, median 952 seconds, worst 1200 seconds",
    ]
    assert actions == [(10, 1), (NOW - HOUR_MS, NOW)]


def test_runner_wait_finding_prints_its_label_value_and_label_class(actions, capsys):
    capsys.readouterr()
    assert doctor.main([WATCHED, "measure", 'ci-runner-wait/20-1-["anton-oss-light"]']) == 0
    assert lines(capsys) == [
        'ci-runner-wait/20-1-["anton-oss-light"] 440',
        "ci-runner-wait anton-oss-light, window 2026-10-17T21:00:00Z to 2026-10-17T22:00:00Z: "
        "1 of 1 runs over 300 seconds, median 440 seconds, worst 440 seconds",
    ]


def test_bare_class_prints_every_scope_over_the_stated_window(actions, capsys):
    capsys.readouterr()
    flags = ["--since", "2026-10-17T19:00Z", "--until", "2026-10-17T21:00Z"]
    assert doctor.main([WATCHED, "measure", "ci-tier-wall-time", *flags]) == 0
    assert lines(capsys) == [
        "ci-tier-wall-time pull request, window 2026-10-17T19:00:00Z to 2026-10-17T21:00:00Z: "
        "2 of 3 runs over 900 seconds, median 952 seconds, worst 1200 seconds",
        "ci-tier-wall-time queue, window 2026-10-17T19:00:00Z to 2026-10-17T21:00:00Z: "
        "1 of 1 runs over 900 seconds, median 2000 seconds, worst 2000 seconds",
    ]
    assert actions == [(NOW - 3 * HOUR_MS, NOW - HOUR_MS)]


@pytest.mark.parametrize(
    "finding, expected",
    [
        ("health/f1", None),
        ("ci-tier-wall-time", ci_measure.Target("ci-tier-wall-time")),
        ("ci-tier-wall-time/10-2", ci_measure.Target("ci-tier-wall-time", 10, 2)),
        ('ci-runner-wait/20-1-["a-b", "c"]', ci_measure.Target("ci-runner-wait", 20, 1, ("a-b", "c"))),
        ("ci-queue-age-unconfirmed/20-3-117", ci_measure.Target("ci-queue-age-unconfirmed", 20, 3)),
    ],
)
def test_targets_parse_run_attempt_and_labels(finding, expected):
    assert ci_measure.target(finding) == expected


@pytest.mark.parametrize("finding", ["ci-tier-wall-time/x-1", "ci-tier-wall-time/10", 'ci-runner-wait/1-1-["a"'])
def test_a_subject_without_a_run_attempt_is_refused(finding):
    with pytest.raises(SwarmError, match="names no run attempt"):
        ci_measure.target(finding)


def test_an_old_subject_without_labels_takes_the_longest_wait_of_the_run():
    goal = ci_measure.target("ci-runner-wait/20-1-117")
    assert ci_measure.value(goal, WAITED, NOW) == (440, None)


def test_waits_split_started_jobs_from_jobs_that_never_started():
    queued = run(21, [job(-700, None), job(-900, -500), job(-2000, None, completed=-1000)])
    assert ci_measure.waits(queued, "ci-runner-wait", NOW) == {("anton-oss",): 400000}
    assert ci_measure.waits(queued, "ci-queue-age-unconfirmed", NOW) == {("anton-oss",): 1000000}
    assert ci_measure.waits(queued, "ci-queue-age-unconfirmed", NOW + 3600000) == {("anton-oss",): 4300000}


def test_waits_group_by_the_sorted_distinct_label_set():
    mixed = run(23, [job(-900, -500, ("b", "a", "b")), job(-900, -700, ("a", "b"))])
    assert ci_measure.waits(mixed, "ci-runner-wait", NOW) == {("a", "b"): 400000}
    assert ci_measure.samples("ci-runner-wait", [mixed], NOW) == {"a, b": [400000]}


def test_an_old_subject_floors_the_longest_wait_to_whole_seconds():
    goal = ci_measure.target("ci-runner-wait/24-1-117")
    assert ci_measure.value(goal, run(24, [job(-1000, -499.5)]), NOW) == (500, None)


def test_a_job_that_started_at_once_waited_zero():
    assert ci_measure.waits(run(25, [job(-100, -100)]), "ci-runner-wait", NOW) == {("anton-oss",): 0}


ACTIVE = {**run(26, [job(-1000, -990, name="Gate — Required"), job(-700, None)]), "status": "in_progress"}


def test_run_values_count_unfinished_work_up_to_now():
    assert ci_measure.value(ci_measure.target("ci-tier-wall-time/26-1"), ACTIVE, NOW) == (1000, "pull request")
    queued = ci_measure.target('ci-queue-age-unconfirmed/26-1-["anton-oss"]')
    assert ci_measure.value(queued, ACTIVE, NOW) == (700, "anton-oss")
    assert ci_measure.samples("ci-tier-wall-time", [ACTIVE], NOW) == {"pull request": [1000000]}


def test_measure_reads_the_run_value_at_now(monkeypatch):
    monkeypatch.setattr(ci_measure.ci_speed_read, "attempt", lambda *args: ACTIVE)
    monkeypatch.setattr(ci_measure.ci_speed_read, "read", lambda *args, **kwargs: {"complete": True, "runs": []})
    finding = 'ci-queue-age-unconfirmed/26-1-["anton-oss"]'
    assert next(ci_measure.measure(finding, ci_measure.target(finding), (NOW - HOUR_MS, NOW), NOW)) == f"{finding} 700"


def test_the_command_measures_an_unfinished_run_at_its_clock(actions, monkeypatch, capsys):
    capsys.readouterr()
    monkeypatch.setattr("scripts.doctor.ci_speed_read.attempt", lambda *args: ACTIVE)
    finding = 'ci-queue-age-unconfirmed/26-1-["anton-oss"]'
    assert doctor.main([WATCHED, "measure", finding]) == 0
    assert lines(capsys)[0] == f"{finding} 700"


def test_window_times_print_in_utc_whatever_the_local_zone(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    time.tzset()
    try:
        assert ci_measure.line("ci-runner-wait", "a", [], (NOW - HOUR_MS, NOW)).startswith(
            "ci-runner-wait a, window 2026-10-17T21:00:00Z to 2026-10-17T22:00:00Z"
        )
    finally:
        monkeypatch.undo()
        time.tzset()


def test_class_counts_compare_milliseconds_with_the_detector_threshold_and_take_the_low_median():
    values = [900000, 900500, 400000, 1200000]
    assert ci_measure.line("ci-tier-wall-time", "queue", values, (NOW - HOUR_MS, NOW)) == (
        "ci-tier-wall-time queue, window 2026-10-17T21:00:00Z to 2026-10-17T22:00:00Z: "
        "2 of 4 runs over 900 seconds, median 900 seconds, worst 1200 seconds"
    )
    assert ci_measure.line("ci-runner-wait", "a", [300000, 300001], (NOW - HOUR_MS, NOW)).endswith(
        "1 of 2 runs over 300 seconds, median 300 seconds, worst 300 seconds"
    )
    assert ci_measure.line("ci-runner-wait", "a", [100000, 700000], (NOW - HOUR_MS, NOW)).endswith(
        "median 100 seconds, worst 700 seconds"
    )


def test_a_run_scope_absent_from_the_window_prints_zero_runs(actions, monkeypatch, capsys):
    capsys.readouterr()
    monkeypatch.setattr(
        "scripts.doctor.ci_speed_read.read",
        lambda *args, **kwargs: {"complete": True, "runs": [wall(13, 2000, "merge_group")]},
    )
    assert doctor.main([WATCHED, "measure", "ci-tier-wall-time/10-1"]) == 0
    assert lines(capsys) == [
        "ci-tier-wall-time/10-1 952",
        "ci-tier-wall-time pull request, window 2026-10-17T21:00:00Z to 2026-10-17T22:00:00Z: "
        "0 of 0 runs over 900 seconds",
    ]


@pytest.mark.parametrize(
    "finding, record, error",
    [
        ("ci-tier-wall-time/20-1", WAITED, "run 20 attempt 1 has no tier wall time"),
        ('ci-runner-wait/20-1-["x"]', WAITED, 'run 20 attempt 1 has no job on runner labels \\["x"\\]'),
        ("ci-runner-wait/22-1-1", run(22, []), "run 22 attempt 1 has no job on runner labels null"),
    ],
)
def test_a_run_without_the_finding_value_is_refused_never_zero(finding, record, error):
    with pytest.raises(SwarmError, match=error):
        ci_measure.value(ci_measure.target(finding), record, NOW)


def test_a_scope_with_no_runs_in_the_window_prints_only_its_counts():
    assert ci_measure.line("ci-runner-wait", "anton-batch", [], (NOW - HOUR_MS, NOW)) == (
        "ci-runner-wait anton-batch, window 2026-10-17T21:00:00Z to 2026-10-17T22:00:00Z: 0 of 0 runs over 300 seconds"
    )


def test_unassigned_jobs_are_named_and_unrated_runs_are_skipped():
    other = {**wall(30, 1000), "event": "push"}
    empty = run(31, [job(-400, -50, ())])
    assert ci_measure.samples("ci-tier-wall-time", [other, empty], NOW) == {}
    assert ci_measure.samples("ci-runner-wait", [empty], NOW) == {"unassigned": [350000]}


def test_an_unreadable_run_is_refused(monkeypatch):
    def attempt(*args):
        raise RuntimeError("Actions API request or time budget exhausted")

    monkeypatch.setattr(ci_measure.ci_speed_read, "attempt", attempt)
    with pytest.raises(SwarmError, match="run 10 attempt 1 unreadable: RuntimeError: Actions API request"):
        list(ci_measure.measure("ci-tier-wall-time/10-1", ci_measure.Target(ci_measure.WALL, 10), (0, NOW), NOW))


def test_an_incomplete_window_read_is_refused_with_its_error(monkeypatch):
    seen = []

    def read(since, until, **kwargs):
        seen.append((since, until, kwargs))
        return {"complete": False, "error": "Actions API read failed: boom", "runs": []}

    monkeypatch.setattr(ci_measure.ci_speed_read, "read", read)
    with pytest.raises(SwarmError, match="Actions API read failed: boom"):
        list(ci_measure.measure("ci-tier-wall-time", ci_measure.Target(ci_measure.WALL), (1, NOW), NOW))
    assert seen == [(1, NOW, {"active": False, "budget": (2000, 600)})]


def test_the_run_value_prints_before_a_failed_window_read(actions, monkeypatch, capsys):
    capsys.readouterr()
    monkeypatch.setattr(
        "scripts.doctor.ci_speed_read.read", lambda *args, **kwargs: {"complete": False, "error": "budget"}
    )
    assert doctor.main([WATCHED, "measure", "ci-tier-wall-time/10-1"]) == 1
    output = capsys.readouterr()
    assert output.out == "ci-tier-wall-time/10-1 952\n"
    assert output.err == "doctor: budget\n"


def test_a_class_with_no_runs_still_prints_its_window(monkeypatch):
    monkeypatch.setattr(ci_measure.ci_speed_read, "read", lambda *args, **kwargs: {"complete": True, "runs": []})
    assert list(
        ci_measure.measure("ci-runner-wait", ci_measure.Target("ci-runner-wait"), (NOW - HOUR_MS, NOW), NOW)
    ) == ["ci-runner-wait all, window 2026-10-17T21:00:00Z to 2026-10-17T22:00:00Z: 0 of 0 runs over 300 seconds"]


def test_the_class_reads_active_jobs_up_to_now_not_the_window_end(monkeypatch):
    monkeypatch.setattr(
        ci_measure.ci_speed_read,
        "read",
        lambda *args, **kwargs: {"complete": True, "runs": [run(40, [job(-HOUR_MS // 1000 - 400, None)])]},
    )
    goal = ci_measure.Target("ci-queue-age-unconfirmed")
    assert list(ci_measure.measure("ci-queue-age-unconfirmed", goal, (0, NOW - HOUR_MS), NOW)) == [
        "ci-queue-age-unconfirmed anton-oss, window 1970-01-01T00:00:00Z to 2026-10-17T21:00:00Z: "
        "1 of 1 runs over 900 seconds, median 4000 seconds, worst 4000 seconds"
    ]
