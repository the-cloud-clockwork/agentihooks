import json
from pathlib import Path

import pytest

from hooks.classifier.decision_log import state_digest
from scripts.gates import intent, intent_calibration

CORPUS = Path(__file__).parents[1] / "fixtures" / "intent_calibration.json"
CONTROLS_BEFORE = ["dq1-no-callsite", "dq1-off", "g18-draft", "g18-off", "pn1-off"]


@pytest.fixture
def corpus():
    return json.loads(CORPUS.read_text())


def test_every_case_is_an_exact_input_labelled_by_both_readers(corpus):
    for case in corpus["cases"]:
        assert state_digest(case["state"]) == case["input_digest"]
        assert case["expected"] in ("pass", "fail")
        assert set(case["labels"]) == {"standards", "spec"}
        assert all(case["labels"].values())
        assert case["control"] is (case["expected"] == "fail" and not case["id"].startswith("retained-"))
        questions = list(intent.questions_for(case["state"]))
        assert [list(sample["answers"]) for sample in case["samples"]["after"]] == [questions] * 3
        assert len(case["samples"]["before"]) == 3


def test_retained_cases_replay_the_exact_classifier_input(corpus):
    retained = [case for case in corpus["cases"] if case["provenance"] == "retained exact masked classifier input"]
    assert [case["id"] for case in retained] == ["retained-t10", "retained-t55", "retained-nd5", "retained-tc22"]
    assert [case["expected"] for case in retained] == ["pass", "fail", "pass", "pass"]


def test_the_weakens_and_chunk_questions_lower_wrong_verdicts_and_keep_every_control(corpus):
    assert intent_calibration.measure(corpus) == {
        "cases": 19,
        "controls": 10,
        "before": {
            "samples": 57,
            "wrong": 15,
            "wrong_cases": [
                "chunk-added",
                "chunk-missing",
                "g18-gates-off",
                "g18-quiet-week",
                "pn1-unpublished",
                "retained-t55",
            ],
            "controls_rejected": CONTROLS_BEFORE,
        },
        "after": {
            "samples": 57,
            "wrong": 2,
            "wrong_cases": ["g18-quiet-week"],
            "controls_rejected": sorted(
                [*CONTROLS_BEFORE, "chunk-added", "chunk-missing", "g18-gates-off", "pn1-unpublished"]
            ),
        },
        "calibrated": True,
    }


def test_the_plan_chunk_cases_pass_the_exact_pull_request_and_fail_the_missing_and_added_ones(corpus):
    cases = {case["id"]: case for case in corpus["cases"] if "plan_chunk" in case["state"]}
    assert {cid: intent_calibration.verdicts(case, "after") for cid, case in cases.items()} == {
        "chunk-exact": ["pass"] * 3,
        "chunk-missing": ["fail"] * 3,
        "chunk-added": ["fail"] * 3,
    }
    assert {cid: intent_calibration.verdicts(case, "before") for cid, case in cases.items()} == {
        cid: ["pass"] * 3 for cid in cases
    }


def test_the_chunk_failures_quote_the_lines_missed_or_exceeded(corpus):
    cases = {case["id"]: case for case in corpus["cases"]}
    first = cases["chunk-missing"]["samples"]["after"][0]["answers"]
    missed = intent.judge(cases["chunk-missing"]["state"], decide=intent_calibration.recorded(first))[1]
    first = cases["chunk-added"]["samples"]["after"][0]["answers"]
    exceeded = intent.judge(cases["chunk-added"]["state"], decide=intent_calibration.recorded(first))[1]
    line = 'line 52 "- It refuses any Read, Grep, Bash or fetch that targets a `plan: true` artifact'
    assert "Deliver every item of plan lines 50-56: " in missed and line in missed
    assert "Remove the scope beyond plan lines 50-56, which ask only for " in exceeded and line in exceeded


def test_losing_a_control_is_not_a_calibration(corpus):
    control = next(case for case in corpus["cases"] if case["id"] == "pn1-off")
    control["samples"]["after"][0]["answers"] = {"usable": 0.9, "delivers": 0.9, "reachable": 0.9, "weakens": 0.1}
    result = intent_calibration.measure(corpus)
    assert (result["after"]["wrong"], "pn1-off" in result["after"]["controls_rejected"]) == (3, False)
    assert result["calibrated"] is False


def test_no_fewer_wrong_verdicts_is_not_a_calibration(corpus):
    for case in corpus["cases"]:
        neutral = {"weakens": 0.0, "underdelivers": 0.0, "overdelivers": 0.0}
        case["samples"]["after"] = [{"answers": {**s["answers"], **neutral}} for s in case["samples"]["before"]]
    result = intent_calibration.measure(corpus)
    assert (result["before"]["wrong"], result["after"]["wrong"], result["calibrated"]) == (15, 15, False)


def tiny(after_control, after_case):
    def case(cid, control, expected, before, after):
        return {
            "id": cid,
            "control": control,
            "expected": expected,
            "state": {},
            "samples": {
                "before": [{"verdict": before}],
                "after": [{"answers": {"usable": after, "delivers": 0.9, "reachable": 0.9, "weakens": 0.0}}],
            },
        }

    return {"cases": [case("c", True, "fail", "fail", after_control), case("p", False, "pass", "fail", after_case)]}


def test_the_same_controls_with_fewer_wrong_verdicts_is_a_calibration():
    assert intent_calibration.measure(tiny(0.1, 0.9)) == {
        "cases": 2,
        "controls": 1,
        "before": {"samples": 2, "wrong": 1, "wrong_cases": ["p"], "controls_rejected": ["c"]},
        "after": {"samples": 2, "wrong": 0, "wrong_cases": [], "controls_rejected": ["c"]},
        "calibrated": True,
    }


def test_main_prints_the_measurement_of_the_default_corpus(corpus, capsys):
    assert intent_calibration.main([]) == 0
    assert capsys.readouterr().out == json.dumps(intent_calibration.measure(corpus), indent=1) + "\n"


def test_main_reads_the_corpus_named_on_the_command_line(tmp_path, monkeypatch, capsys):
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps(tiny(0.1, 0.9)))
    monkeypatch.setattr("sys.argv", ["intent_calibration", str(path)])
    assert intent_calibration.main() == 0
    assert capsys.readouterr().out == json.dumps(intent_calibration.measure(tiny(0.1, 0.9)), indent=1) + "\n"
