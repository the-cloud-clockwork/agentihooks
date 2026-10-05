import copy

from scripts.doctor import ci
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
    assert ci.red_checks(record) == []
    planted = copy.deepcopy(record)
    planted["checks"][0]["conclusion"] = "failure"
    [found] = ci.red_checks(planted)
    assert found.id == "red-checks/487"
    assert found.measure == 1
    assert "record-pass: failure" in found.evidence[0]
    assert planted["checks"][0]["html_url"] in found.evidence[0]
    planted["checks"][0]["head_sha"] = "older"
    assert ci.red_checks(planted) == []


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
