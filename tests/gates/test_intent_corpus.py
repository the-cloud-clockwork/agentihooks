import json

import pytest
import yaml

from hooks.classifier import corpus, definitions, evaluation
from hooks.classifier.decision_log import state_digest
from hooks.classifier.result import Answer, DecisionResult
from scripts.gates import intent, intent_calibration

PHASE = (
    "Plan chunks: agents read only their slice of the plan: Operator top priority. Every planner plan is stored as a "
    "ledger artifact. Each phase points at its plan and each task at a line range computed by code from slice "
    "anchors. Agents read only their range, ten lines of margin each side, through one command, and a hook refuses "
    "whole plan reads from engineer and ci agents. The intent check judges every pull request against its exact "
    "chunk and fails both underdelivery and overdelivery. Done when a proof swarm shows the refused whole read, the "
    "chunk read, and a failed intent verdict on an overreaching pull request."
)
CONTROLS_BEFORE = ["dq1-no-callsite", "dq1-off", "g18-draft", "g18-off", "pn1-off"]


@pytest.fixture
def definition():
    return definitions.load(intent.PURPOSE)


@pytest.fixture
def raw():
    return yaml.safe_load(corpus.path_for(intent.PURPOSE).read_text())


@pytest.fixture
def cases(definition):
    return corpus.load(definition, corpus.path_for(intent.PURPOSE))


def write(tmp_path, raw):
    path = tmp_path / "intent-check.corpus.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


def verdicts(definition, case):
    return [item.verdicts["verdict"] for item in evaluation.replay(definition, (case,))]


def test_every_case_is_an_exact_input_labelled_by_both_readers(cases):
    for case in cases:
        assert state_digest(case.state) == case.notes["input_digest"]
        assert case.expected["verdict"] in ("pass", "fail")
        assert set(case.notes["labels"]) == {"standards", "spec"}
        assert all(case.notes["labels"].values())
        assert case.control is (case.expected["verdict"] == "fail" and not case.name.startswith("retained-"))
        questions = list(intent.questions_for(case.state))
        assert [list(sample.answers) for sample in case.samples] == [questions] * 3
        assert len(case.baseline) == 3


def test_retained_cases_replay_the_exact_classifier_input(cases):
    retained = [case for case in cases if case.notes["provenance"] == "retained exact masked classifier input"]
    assert [case.name for case in retained] == ["retained-t10", "retained-t55", "retained-nd5", "retained-tc22"]
    assert [case.expected["verdict"] for case in retained] == ["pass", "fail", "pass", "pass"]


def test_the_weakens_and_chunk_questions_lower_wrong_verdicts_and_keep_every_control(cases, definition):
    assert intent_calibration.measure(cases, definition) == {
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


def test_the_eval_replay_gives_the_calibration_counts(definition):
    report = evaluation.evaluate(intent.PURPOSE).report()
    measured = intent_calibration.measure(corpus.load(definition, corpus.path_for(intent.PURPOSE)), definition)
    assert (report["mode"], report["cases"], report["controls"], report["samples"]) == ("replay", 19, 10, 57)
    assert (report["wrong"], report["wrong_cases"]) == (measured["after"]["wrong"], measured["after"]["wrong_cases"])
    assert report["held_controls"] == measured["after"]["controls_rejected"]


def test_the_plan_chunk_cases_pass_the_exact_pull_request_and_fail_the_missing_and_added_ones(cases, definition):
    chunked = {case.name: case for case in cases if "plan_chunk" in case.state}
    assert {name: verdicts(definition, case) for name, case in chunked.items()} == {
        "chunk-exact": ["pass"] * 3,
        "chunk-missing": ["fail"] * 3,
        "chunk-added": ["fail"] * 3,
    }
    assert {name: [item["verdict"] for item in case.baseline] for name, case in chunked.items()} == {
        name: ["pass"] * 3 for name in chunked
    }


def test_the_chunk_failures_quote_the_lines_missed_or_exceeded(cases, definition):
    named = {case.name: case for case in cases}
    missed, exceeded = (
        evaluation.replay(definition, (named[name],))[0].verdicts["reason"] for name in ("chunk-missing", "chunk-added")
    )
    rows = named["chunk-added"].state["plan_chunk"].splitlines()
    every = ", ".join(f'line {number} "{row}"' for number, row in enumerate(rows, 50))
    remove = f"Remove the scope beyond plan lines 50-56, which ask only for {every}."
    deliver = (
        f'Deliver what plan lines 50-56 ask for and the change leaves out: line 52 "{rows[2]}", line 56 "{rows[6]}".'
    )
    assert missed.endswith(f". {deliver} {remove}")
    assert exceeded.endswith(f"the pull request merges.. The phase must be able to use it for {PHASE}. {remove}")


def test_losing_a_control_is_not_a_calibration(raw, definition, tmp_path):
    control = next(case for case in raw["cases"] if case["name"] == "pn1-off")
    accepted = {"usable": 0.9, "delivers": 0.9, "reachable": 0.9, "weakens": 0.1}
    control["samples"][0]["answers"] = {key: {"type": "noul", "noul": value} for key, value in accepted.items()}
    result = intent_calibration.measure(corpus.load(definition, write(tmp_path, raw)), definition)
    assert (result["after"]["wrong"], "pn1-off" in result["after"]["controls_rejected"]) == (3, False)
    assert result["calibrated"] is False


def test_no_fewer_wrong_verdicts_is_not_a_calibration(raw, cases, definition, tmp_path):
    replayed = {}
    for item in evaluation.replay(definition, cases):
        replayed.setdefault(item.case.name, []).append({"verdict": item.verdicts["verdict"]})
    for case in raw["cases"]:
        case["baseline"] = replayed[case["name"]]
    result = intent_calibration.measure(corpus.load(definition, write(tmp_path, raw)), definition)
    assert (result["before"]["wrong"], result["after"]["wrong"], result["calibrated"]) == (2, 2, False)


def test_a_wrong_baseline_with_every_control_held_is_a_calibration(raw, definition, tmp_path):
    for case in raw["cases"]:
        case["baseline"] = [{"verdict": "fail" if case["expected"]["verdict"] == "pass" else "pass"}] * 3
        case["control"] = False
    result = intent_calibration.measure(corpus.load(definition, write(tmp_path, raw)), definition)
    assert (result["before"]["wrong"], result["after"]["wrong"], result["calibrated"]) == (57, 2, True)


def answered(**values):
    def decide(state, questions, purpose):
        return DecisionResult({name: Answer("noul", noul=values.get(name, 0.9)) for name in questions}, "stub")

    return decide


@pytest.mark.parametrize(
    "state",
    [{}, {"task_part": "tests-first", "pull_request_diff": ""}],
)
def test_a_threshold_override_through_the_environment_changes_the_intent_verdict(state, monkeypatch):
    decide = answered(usable=0.5, weakens=0.1, changes_gate_behavior=0.1)
    assert intent.judge(state, decide=decide)[0] == "pass"
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_INTENT_CHECK_FAIL", "0.6")
    verdict, reason = intent.judge(state, decide=decide)
    assert (verdict, reason.startswith("the phase can use this change at probability 0.50, under 0.6")) == (
        "fail",
        True,
    )


def tiny(after_control, after_case):
    def case(name, control, expected, before, after):
        answers = {"usable": after, "delivers": 0.9, "reachable": 0.9, "weakens": 0.0}
        return {
            "name": name,
            "control": control,
            "expected": {"verdict": expected},
            "state": {},
            "baseline": [{"verdict": before}],
            "samples": [
                {"source": "unrecorded", "answers": {key: {"type": "noul", "noul": v} for key, v in answers.items()}}
            ],
        }

    return {
        "version": 1,
        "cases": [case("c", True, "fail", "fail", after_control), case("p", False, "pass", "fail", after_case)],
    }


def test_the_same_controls_with_fewer_wrong_verdicts_is_a_calibration(definition, tmp_path):
    cases = corpus.load(definition, write(tmp_path, tiny(0.1, 0.9)))
    assert intent_calibration.measure(cases) == {
        "cases": 2,
        "controls": 1,
        "before": {"samples": 2, "wrong": 1, "wrong_cases": ["p"], "controls_rejected": ["c"]},
        "after": {"samples": 2, "wrong": 0, "wrong_cases": [], "controls_rejected": ["c"]},
        "calibrated": True,
    }


def test_main_prints_the_measurement_of_the_packaged_corpus(cases, definition, capsys):
    assert intent_calibration.main([]) == 0
    assert capsys.readouterr().out == json.dumps(intent_calibration.measure(cases, definition), indent=1) + "\n"


def test_main_reads_the_corpus_named_on_the_command_line(definition, tmp_path, monkeypatch, capsys):
    path = write(tmp_path, tiny(0.1, 0.9))
    monkeypatch.setattr("sys.argv", ["intent_calibration", str(path)])
    assert intent_calibration.main() == 0
    expected = intent_calibration.measure(corpus.load(definition, path), definition)
    assert capsys.readouterr().out == json.dumps(expected, indent=1) + "\n"
