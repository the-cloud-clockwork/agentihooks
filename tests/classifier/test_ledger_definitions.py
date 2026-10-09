from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hooks import config
from hooks.classifier import Answer, DecisionResult, definitions, runner
from hooks.classifier.questions import Score, YesNo
from hooks.context import profile_chain
from hooks.filters import runner as filter_runner
from scripts.swarm import slice_screen, trace_plan
from scripts.swarm_ledger import ledger_duplicates

SIZES = ["trivial", "one pull request", "several pull requests", "a whole phase"]
TRACE_SIZE = (
    "How much source work is the plan? Judge only pieces with nonempty areas; test only pieces and the shared "
    "mutation clearance file are supporting proof, not additional scope."
)
CASES = [
    (
        "ledger-duplicate",
        "cli",
        {"same": 0.6},
        {"pairs": [{"new": 0, "slot": 1, "kind": "task", "id": "t1", "title": "Publish {the} wheel"}]},
        {
            "new_0_existing_1": YesNo(
                "Does new item 0 ask for the same change as task t1 titled Publish {the} wheel?",
                "the new item asks for the same change as the existing item",
                "the new item asks for a different change",
            )
        },
    ),
    (
        "filter",
        "none",
        {"confirm": 0.5},
        {"question": "Q?", "intent": "no ids", "findings": [{"text": "t1", "reason": "an id", "context": "c t1"}]},
        {
            "finding_0": YesNo(
                "Q?\nIntent: no ids\nFinding: t1\nReason: an id\nContext: c t1",
                "yes, the finding goes against the intent",
                "no, the finding is fine",
            )
        },
    ),
    (
        "phase-slice",
        "cli",
        {"off_intent": 0.3},
        {"tasks": [{"id": "t1", "title": "One"}, {"id": "t2", "title": "Two"}]},
        {
            "size_0": Score("How much work is task t1, titled One?", SIZES),
            "serves_0": YesNo(
                "Does task t1, titled One, serve the phase intent?", "it advances the phase", "it serves something else"
            ),
            "size_1": Score("How much work is task t2, titled Two?", SIZES),
            "serves_1": YesNo(
                "Does task t2, titled Two, serve the phase intent?", "it advances the phase", "it serves something else"
            ),
        },
    ),
    (
        "trace-plan",
        "cli",
        {"off_intent": 0.3, "too_big_confidence": 0.7},
        {"pieces": [{"slot": 2, "number": 3, "what": "a light"}], "sized": [{}]},
        {
            "piece_2": YesNo(
                "Is piece 3, a light, needed to deliver the task?", "the task needs it", "it serves something else"
            ),
            "size": Score(TRACE_SIZE, SIZES),
        },
    ),
]


@pytest.fixture
def packaged(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENTIHOOKS_HOME", tmp_path)
    with patch.object(profile_chain, "read_state", return_value={}):
        yield


@pytest.mark.parametrize(("name", "fallbacks", "thresholds", "params", "questions"), CASES)
def test_ledger_and_filter_definitions_reproduce_the_call_site_prompts(
    packaged, name, fallbacks, thresholds, params, questions
):
    definition = definitions.load(name, environ={})
    assert (definition.purpose, definition.fallbacks, definition.thresholds) == (name, fallbacks, thresholds)
    assert definition.rule.type == ("yes" if name == "filter" else "code")
    assert list(runner.questions_for(definition, params).items()) == list(questions.items())


def test_the_filter_verdict_rule_confirms_at_its_threshold(packaged):
    assert definitions.load("filter", environ={}).rule.threshold == "confirm"


def _judge(probability):
    def decide(state, questions, **kwargs):
        return DecisionResult({name: Answer("noul", noul=probability) for name in questions}, "unit-test")

    return decide


def _duplicates():
    return {"tasks": [{"id": "t1", "title": "Publish the wheel to PyPI", "state": "open"}]}


def test_the_ledger_duplicate_line_comes_from_its_definition(packaged, monkeypatch):
    item = [{"title": "Publish the wheel to PyPI"}]
    assert ledger_duplicates.find(_duplicates(), "task", item, judge=_judge(0.9))[0].id == "t1"
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_LEDGER_DUPLICATE_SAME", "0.95")
    assert ledger_duplicates.find(_duplicates(), "task", item, judge=_judge(0.9)) == [None]


def test_the_filter_confirm_line_comes_from_its_definition(packaged, monkeypatch):
    spec = SimpleNamespace(mode="classifier", intent_from=None, intent="no ids", question="Q?")
    findings = [filter_runner.Finding((), 0, 2, "t1", "an id")]
    monkeypatch.setattr("hooks.classifier.decide", _judge(0.5))
    entry, payload = {"file": "f.filter.yaml", "path": "f"}, {"tool_input": {}}
    assert filter_runner.confirm(entry, spec, payload, findings) == findings
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_FILTER_CONFIRM", "0.6")
    assert filter_runner.confirm(entry, spec, payload, findings) == []


def test_the_slice_screen_off_intent_line_comes_from_its_definition(packaged, monkeypatch):
    answers = {"size_0": Answer("score", score=1.0, confidence=0.9), "serves_0": Answer("noul", noul=0.4)}
    task = [{"id": "t1"}]
    assert slice_screen.flags(answers, task, 0.7) == []
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_PHASE_SLICE_OFF_INTENT", "0.5")
    assert slice_screen.flags(answers, task, 0.7) == [
        "Classifier: task t1 may be off intent, serves the phase at probability 0.40."
    ]


def _trace(monkeypatch, p_yes, score, confidence):
    answers = {f"piece_{i}": Answer("noul", noul=p) for i, p in enumerate(p_yes)}
    answers["size"] = Answer("score", score=score, confidence=confidence)
    monkeypatch.setattr(trace_plan, "decide", lambda state, questions, **kwargs: DecisionResult(answers, "stub"))
    pieces = [trace_plan.Piece(f"piece {i}", ("src",), "why") for i in range(len(p_yes))]
    return trace_plan.trace(pieces, {}, None)


def test_the_trace_plan_cut_rule_reads_its_thresholds_from_the_definition(packaged, monkeypatch):
    record = _trace(monkeypatch, (0.4, 0.9), 2.0, 0.8)
    assert ([row["kept"] for row in record["pieces"]], record["verdict"]) == ([True, True], "fail")
    assert record["size"] == {"level": 2, "name": "several pull requests", "confidence": 0.8}
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_TRACE_PLAN_OFF_INTENT", "0.5")
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_TRACE_PLAN_TOO_BIG_CONFIDENCE", "0.85")
    record = _trace(monkeypatch, (0.4, 0.9), 2.0, 0.8)
    assert ([row["kept"] for row in record["pieces"]], record["verdict"]) == ([False, True], "pass")


def test_the_size_names_come_from_the_definition_levels(packaged):
    assert slice_screen.SIZES == trace_plan.SIZES == SIZES
    assert slice_screen.SIZES[slice_screen.ONE_PR] == "one pull request"
    assert (filter_runner.TRUE, filter_runner.FALSE) == (
        "yes, the finding goes against the intent",
        "no, the finding is fine",
    )


@pytest.mark.parametrize("module", [slice_screen, trace_plan, filter_runner])
def test_an_unknown_module_attribute_is_still_missing(packaged, module):
    assert hasattr(module, "NOPE") is False


def test_slice_flags_use_the_definition_the_screen_asked_with(packaged):
    answers = {"size_0": Answer("score", score=1.0, confidence=0.9), "serves_0": Answer("noul", noul=0.4)}
    asked = definitions.load("phase-slice", environ={})
    stricter = replace(asked, thresholds={"off_intent": 0.5})
    assert slice_screen.flags(answers, [{"id": "t1"}], 0.7, asked) == []
    assert slice_screen.flags(answers, [{"id": "t1"}], 0.7, stricter) == [
        "Classifier: task t1 may be off intent, serves the phase at probability 0.40."
    ]


def test_the_screen_hands_its_run_definition_to_the_flags(packaged, monkeypatch):
    answers = {"size_0": Answer("score", score=1.0, confidence=0.9), "serves_0": Answer("noul", noul=0.2)}
    monkeypatch.setattr(slice_screen, "decide", lambda state, questions, **kwargs: DecisionResult(answers, "stub"))
    monkeypatch.setattr(definitions, "load", lambda name, **kwargs: pytest.fail("flags reloaded the definition"))
    phase = {"id": "p1", "title": "Build", "description": "Intent."}
    plan = {"id": "plan-p1", "phase": "p1", "kind": "plan", "lane": "plan", "proof": {"slice": "t1"}}
    task = {"id": "t1", "phase": "p1", "title": "One", "description": "Work."}
    doc = {"overview": "Ship.", "phases": [phase], "tasks": [plan, task]}
    assert slice_screen.screen(phase, doc, 0.7).flags == (
        "Classifier: task t1 may be off intent, serves the phase at probability 0.20.",
    )
