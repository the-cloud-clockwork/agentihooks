import json
from pathlib import Path

import pytest

from hooks.classifier import definitions
from hooks.classifier.result import Answer
from scripts.gates import intent

COHORT = Path(__file__).parents[1] / "fixtures" / "intent" / "phase_fit_cohort.json"


@pytest.fixture(scope="module")
def rows():
    return json.loads(COHORT.read_text())["rows"]


def judged(state, sample):
    answers = {key: Answer.from_wire(value) for key, value in sample["answers"].items()}
    return intent._verdicts(definitions.load(intent.PURPOSE), state, {}, answers)


def after(row):
    return [judged(row.get("after_state", row["before_state"]), sample) for sample in row["after"]]


def before(row):
    return [judged(row["before_state"], sample) for sample in row["before"]]


def failed(verdicts, criteria):
    return any(item["verdict"] == "fail" and item["criterion"] in criteria for item in verdicts)


def measure(rows):
    cohort = [row for row in rows if not row["control"]]
    sides = {"before": [before(row) for row in cohort], "after": [after(row) for row in cohort]}
    return {
        "rows": len(cohort),
        "plans": sum(row["kind"] == "plan" for row in cohort),
        "production_phase_scope": sum(row["production"]["answers"]["usable"]["noul"] < 0.3 for row in cohort),
        "before_phase_scope": sum(failed(verdicts, ("usable", "phase")) for verdicts in sides["before"]),
        "after_phase_scope": sum(failed(verdicts, ("phase",)) for verdicts in sides["after"]),
        "after_delivery": sum(failed(verdicts, ("delivery",)) for verdicts in sides["after"]),
        "after_other": sum(failed(verdicts, ("coverage", "weakens", "chunk")) for verdicts in sides["after"]),
        "after_plans_passed": sum(
            all(item["verdict"] == "pass" for item in verdicts)
            for row, verdicts in zip(cohort, sides["after"])
            if row["kind"] == "plan"
        ),
    }


def test_no_adjudicated_row_escalates_on_phase_scope_and_delivery_failures_count_apart(rows):
    assert measure(rows) == {
        "rows": 35,
        "plans": 19,
        "production_phase_scope": 35,
        "before_phase_scope": 31,
        "after_phase_scope": 0,
        "after_delivery": 16,
        "after_other": 1,
        "after_plans_passed": 18,
    }


def test_every_negative_control_still_fails_on_the_criterion_both_readers_named(rows):
    controls = [row for row in rows if row["control"]]
    assert len(controls) == 5
    for row in controls:
        expected = {tuple(value) for value in row["expected"].values()}
        assert len(expected) == 1
        [(verdict, criterion)] = expected
        verdicts = after(row)
        assert [(item["verdict"], item["criterion"]) for item in verdicts] == [(verdict, criterion)] * 3, row["name"]
        assert all(item["verdict"] == "fail" for item in before(row))


def test_a_fitting_change_short_of_delivery_is_never_reported_as_a_phase_mismatch(rows):
    for row in rows:
        if row["kind"] == "delivered" and not row["control"]:
            for item in after(row):
                assert item["criterion"] in (None, "delivery"), row["name"]
                assert "may not fit the phase" not in item["reason"]


def test_the_cohort_holds_the_exact_inputs_the_tick_judged(rows):
    for row in rows:
        if not row["control"]:
            assert row["production"]["source"] == "liquid-d1"
            assert row["production"]["definition"] == "intent-check"
