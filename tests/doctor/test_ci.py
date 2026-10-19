import copy

import pytest

from scripts.doctor import ci
from scripts.swarm import ledger_events
from tests.doctor.recorded import load


def test_recorded_ci_and_planted_rerun():
    record = load("ci")
    assert ci.reruns(record) == []
    planted = copy.deepcopy(record)
    planted["runs"][0]["run_attempt"] = 4
    [found] = ci.reruns(planted)
    assert found.id == "ci-reruns/487"
    assert found.measure == 3
    assert "37323835080 attempts 4" in found.evidence
    assert found.threshold == "more than 2 reruns per pull request head"


@pytest.mark.parametrize("attempts", [1, 2, 3])
def test_reruns_at_or_under_the_gate_limit_are_quiet(attempts):
    record = load("ci")
    record["runs"][0]["run_attempt"] = attempts
    assert ci.reruns(record) == []


@pytest.mark.parametrize("cap", [0, 1, 4])
def test_reruns_use_the_gate_setting(monkeypatch, cap):
    gate = ci.RerunBudget(cap=cap)
    monkeypatch.setattr(ci, "RerunBudget", lambda: gate)
    record = load("ci")
    record["runs"][0]["run_attempt"] = cap + 1
    assert ci.reruns(record) == []
    record["runs"][0]["run_attempt"] += 1
    [found] = ci.reruns(record)
    assert found.measure == cap + 1
    assert found.threshold == f"more than {cap} reruns per pull request head"


def test_reruns_count_all_workflows_on_only_the_current_head():
    record = load("ci")
    run = record["runs"][0]
    run["run_attempt"] = 2
    record["runs"].extend(
        [
            {**run, "id": 42, "run_attempt": 3},
            {**run, "id": 43, "head_sha": "previous-head", "run_attempt": 20},
        ]
    )
    [found] = ci.reruns(record)
    assert found.measure == 3
    assert found.evidence == ("37323835080 attempts 2", "42 attempts 3")


def _red_at(record):
    return ledger_events.iso_ms(record["checks"][0]["completed_at"])


def test_recorded_checks_and_planted_red_check():
    record = load("ci")
    later = _red_at(record) + ledger_events.RED_QUIET_MS
    assert ci.red_checks(record, later) == []
    planted = copy.deepcopy(record)
    planted["checks"][0]["conclusion"] = "failure"
    [found] = ci.red_checks(planted, later)
    assert found.id == "red-checks/487"
    assert found.measure == 1
    assert "record-pass: failure" in found.evidence[0]
    assert planted["checks"][0]["html_url"] in found.evidence[0]
    planted["checks"][0]["head_sha"] = "older"
    assert ci.red_checks(planted, later) == []


def _red(record):
    planted = copy.deepcopy(record)
    planted["checks"][0]["conclusion"] = "failure"
    return planted


def test_a_red_head_is_reported_only_after_the_tick_red_window_from_its_red_result():
    planted = _red(load("ci"))
    red = _red_at(planted)
    assert ci.red_checks(planted, red + ledger_events.RED_QUIET_MS - 1) == []
    assert [f.id for f in ci.red_checks(planted, red + ledger_events.RED_QUIET_MS)] == ["red-checks/487"]
    assert [f.id for f in ci.findings(planted, red + ledger_events.RED_QUIET_MS)] == ["red-checks/487"]
    assert ci.findings(planted, red) == []


def test_the_red_window_is_read_from_the_tick_setting(monkeypatch):
    planted = _red(load("ci"))
    monkeypatch.setattr(ledger_events, "RED_QUIET_MS", 5 * 60_000)
    assert ci.red_checks(planted, _red_at(planted) + 5 * 60_000 - 1) == []
    assert len(ci.red_checks(planted, _red_at(planted) + 5 * 60_000)) == 1


def test_a_head_pushed_after_its_red_result_waits_the_window_from_its_push():
    planted = _red(load("ci"))
    pushed = _red_at(planted) + 10 * 60_000
    planted["runs"].append({**planted["runs"][0], "id": 2, "created_at": "2026-10-05T14:40:00Z"})
    assert len(ci.red_checks(planted, pushed + ledger_events.RED_QUIET_MS)) == 1
    planted["runs"][0]["created_at"] = "2026-10-05T14:35:38Z"
    assert ci.red_checks(planted, pushed + ledger_events.RED_QUIET_MS - 1) == []
    assert len(ci.red_checks(planted, pushed + ledger_events.RED_QUIET_MS)) == 1
    planted["runs"] = [{**planted["runs"][0], "head_sha": "other"}]
    assert len(ci.red_checks(planted, _red_at(planted) + ledger_events.RED_QUIET_MS)) == 1


def test_an_old_commit_pushed_now_is_not_reported_before_the_window_from_its_red_result():
    planted = _red(load("ci"))
    planted["committed_at"] = ledger_events.iso_ms("2026-10-05T12:00:00Z")
    assert ci.red_checks(planted, _red_at(planted) + ledger_events.RED_QUIET_MS - 1) == []
    assert len(ci.red_checks(planted, _red_at(planted) + ledger_events.RED_QUIET_MS)) == 1


@pytest.mark.parametrize(
    ("conclusion", "found"),
    [("failure", 1), ("timed_out", 1), ("action_required", 1), ("startup_failure", 1), ("cancelled", 0)],
)
def test_each_red_conclusion_counts_as_a_red_check(conclusion, found):
    planted = load("ci")
    planted["checks"][0]["conclusion"] = conclusion
    assert len(ci.red_checks(planted, _red_at(planted) + ledger_events.RED_QUIET_MS)) == found


def test_the_window_starts_at_the_earliest_red_check():
    planted = _red(load("ci"))
    planted["checks"][1]["conclusion"] = "failure"
    planted["checks"][1]["completed_at"] = "2026-10-05T14:20:00Z"
    first = ledger_events.iso_ms("2026-10-05T14:20:00Z")
    assert ci.red_checks(planted, first + ledger_events.RED_QUIET_MS - 1) == []
    assert ci.red_checks(planted, first + ledger_events.RED_QUIET_MS)[0].measure == 2


def test_a_push_since_the_red_head_restarts_the_window():
    planted = _red(load("ci"))
    stale = _red_at(planted) + 3 * ledger_events.RED_QUIET_MS
    assert len(ci.red_checks(planted, stale)) == 1
    planted["pr"]["head"]["sha"] = "pushed"
    assert ci.red_checks(planted, stale) == []
    planted["checks"][0]["head_sha"] = "pushed"
    planted["checks"][0]["completed_at"] = "2026-10-05T15:25:00Z"
    assert ci.red_checks(planted, stale) == []
    assert len(ci.red_checks(planted, _red_at(planted) + ledger_events.RED_QUIET_MS)) == 1


def test_recorded_tests_and_planted_flake_on_the_same_commit():
    record = load("ci")
    assert len(record["attempts"][0]["tests"]) == 12
    assert ci.flaky_tests(record) == []
    planted = copy.deepcopy(record)
    first = planted["attempts"][0]
    second = copy.deepcopy(first)
    first["tests"][0]["outcome"] = "FAILED"
    second["attempt"] = 2
    planted["attempts"].append(second)
    [found] = ci.flaky_tests(planted)
    assert found.kind == "flaky tests"
    assert found.subject.startswith("487/")
    assert first["tests"][0]["nodeid"] in found.evidence
    assert found.measure == 1
    assert "attempt 1 FAILED, attempt 2 PASSED" in found.evidence
    second["head_sha"] = "different"
    assert ci.flaky_tests(planted) == []


def test_matrix_jobs_and_skipped_tests_cannot_prove_a_flake():
    record = load("ci")
    first = record["attempts"][0]
    second = copy.deepcopy(first)
    first["tests"][0]["outcome"] = "FAILED"
    second["attempt"] = 2
    record["attempts"].append(second)
    second["tests"][0]["job"] = "unit (3.12, 1)"
    assert ci.flaky_tests(record) == []
    second["tests"][0]["job"] = first["tests"][0]["job"]
    second["tests"][0]["outcome"] = "SKIPPED"
    assert ci.flaky_tests(record) == []


HIVE = ".github/workflows/hive-worker.yml"


def _push(run_id, conclusion, path=HIVE, name="Worker hive pod"):
    return {
        "id": run_id,
        "name": name,
        "path": path,
        "conclusion": conclusion,
        "created_at": f"2026-10-18T05:{run_id:02d}:00Z",
        "html_url": f"https://github.com/o/r/actions/runs/{run_id}",
    }


def _dev(runs, green=None, jobs=None):
    return {"runs": runs, "green": green or {}, "jobs": jobs or {}}


def test_five_consecutive_failed_dev_pushes_of_a_workflow_raise_one_finding():
    runs = [_push(n, "failure") for n in range(10, 15)] + [_push(9, "success")]
    green = {HIVE: _push(9, "success")}
    [found] = ci.red_dev_workflows(_dev(runs, green, {14: ["contracts", "pod"]}))
    assert found.id == "red-dev-workflow/hive-worker.yml"
    assert found.summary == "Worker hive pod failed its last 5 dev push runs"
    assert found.evidence == (
        "last green 2026-10-18T05:09:00Z https://github.com/o/r/actions/runs/9",
        "failing jobs in run 14: contracts, pod",
        *(f"https://github.com/o/r/actions/runs/{n}" for n in range(14, 9, -1)),
    )
    assert found.threshold == "5 consecutive failed dev push runs outside the promotion gate"
    assert found.measure == 5


def test_the_streak_counts_every_consecutive_failure_and_names_a_missing_green():
    runs = [_push(n, ("failure", "timed_out", "startup_failure")[n % 3]) for n in range(10, 17)]
    [found] = ci.red_dev_workflows(_dev(runs))
    assert found.measure == 7
    assert found.summary == "Worker hive pod failed its last 7 dev push runs"
    assert found.evidence[:2] == ("no green dev push run found", "failing jobs in run 16: none read")
    assert len(found.evidence) == 9


@pytest.mark.parametrize(
    "runs",
    [
        [_push(15, "success")] + [_push(n, "failure") for n in range(10, 15)],
        [_push(n, "failure") for n in range(10, 14)] + [_push(9, "success")],
        [_push(n, "failure") for n in range(11, 15)],
        [_push(n, "failure", path=".github/workflows/test.yml", name="Tests") for n in range(10, 15)],
    ],
    ids=["latest passed", "four failed", "only four runs", "promotion gate"],
)
def test_no_finding_without_five_consecutive_failures_outside_the_promotion_gate(runs):
    assert ci.red_dev_workflows(_dev(runs)) == []


def test_cancelled_and_skipped_runs_neither_break_nor_extend_a_streak():
    failed = [_push(n, "failure") for n in range(10, 15)]
    runs = [_push(30, "cancelled"), *failed[:3], _push(13, "skipped"), *failed[3:]]
    assert ci.red_streaks(runs) == {HIVE: failed[::-1]}


def test_workflows_are_judged_apart_and_ordered_by_newest_run():
    other = ".github/workflows/swarm-smoke.yml"
    runs = [_push(n, "failure") for n in range(10, 15)]
    runs += [_push(n, "failure", path=other, name="Swarm smoke") for n in range(20, 25)]
    runs += [_push(n, "success", path=".github/workflows/pages.yml", name="Pages") for n in range(30, 36)]
    found = ci.red_dev_workflows(_dev(runs))
    assert [f.subject for f in found] == ["swarm-smoke.yml", "hive-worker.yml"]
