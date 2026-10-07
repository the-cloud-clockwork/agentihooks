import copy

from scripts.doctor import ci
from scripts.swarm import ledger_events
from tests.doctor.recorded import load


def test_recorded_ci_and_planted_rerun():
    record = load("ci")
    assert ci.reruns(record) == []
    planted = copy.deepcopy(record)
    planted["runs"][0]["run_attempt"] = 3
    [found] = ci.reruns(planted)
    assert found.id == "ci-reruns/487"
    assert found.measure == 2
    assert "37323835080 attempts 3" in found.evidence


def test_recorded_checks_and_planted_red_check():
    record = load("ci")
    later = record["committed_at"] + ledger_events.RED_QUIET_MS
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


def test_a_red_head_is_reported_only_after_the_tick_red_window_without_a_push():
    planted = _red(load("ci"))
    committed = planted["committed_at"]
    assert ci.red_checks(planted, committed + ledger_events.RED_QUIET_MS - 1) == []
    assert [f.id for f in ci.red_checks(planted, committed + ledger_events.RED_QUIET_MS)] == ["red-checks/487"]
    assert [f.id for f in ci.findings(planted, committed + ledger_events.RED_QUIET_MS)] == ["red-checks/487"]
    assert ci.findings(planted, committed) == []


def test_the_red_window_is_read_from_the_tick_setting(monkeypatch):
    planted = _red(load("ci"))
    monkeypatch.setattr(ledger_events, "RED_QUIET_MS", 5 * 60_000)
    assert ci.red_checks(planted, planted["committed_at"] + 5 * 60_000 - 1) == []
    assert len(ci.red_checks(planted, planted["committed_at"] + 5 * 60_000)) == 1


def test_a_push_since_the_red_head_restarts_the_window():
    planted = _red(load("ci"))
    stale = planted["committed_at"] + 3 * ledger_events.RED_QUIET_MS
    assert len(ci.red_checks(planted, stale)) == 1
    planted["pr"]["head"]["sha"] = "pushed"
    planted["committed_at"] = stale - 60_000
    assert ci.red_checks(planted, stale) == []
    planted["checks"][0]["head_sha"] = "pushed"
    assert ci.red_checks(planted, stale) == []
    assert len(ci.red_checks(planted, planted["committed_at"] + ledger_events.RED_QUIET_MS)) == 1


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
