from pathlib import Path
from unittest.mock import patch

import pytest

from hooks.classifier import runner
from hooks.classifier.definitions import DefinitionError
from hooks.classifier.questions import Choice, Score, YesNo
from hooks.classifier.result import Answer, DecisionResult
from hooks.context import profile_chain

from .test_definitions import definition_home as definition_home
from .test_definitions import sample, write_definition


@pytest.mark.parametrize(
    ("name", "params", "question", "answer", "verdict"),
    [
        (
            "yes",
            {"instructions": "Accept?", "true": "Yes", "false": "No"},
            YesNo("Accept?", "Yes", "No"),
            Answer("noul", noul=0.5),
            True,
        ),
        (
            "choice",
            {"instructions": "Pick", "first": "One", "second": "Two"},
            Choice("Pick", {"first": "One", "second": "Two"}),
            Answer("choice", choice="second", confidence=0.6),
            "second",
        ),
        (
            "score",
            {"instructions": "Rate", "low": "Low", "high": "High"},
            Score("Rate", ["Low", "High"]),
            Answer("score", score=0.4, confidence=0.6),
            0.4,
        ),
    ],
)
def test_packaged_shapes_run_through_the_shared_runner(
    definition_home, monkeypatch, name, params, question, answer, verdict
):
    monkeypatch.setattr(profile_chain, "BUILT_IN_PROFILES", Path(__file__).resolve().parents[2] / "profiles")
    result = DecisionResult({"verdict": answer}, "stub")
    with patch.object(runner, "decide", return_value=result) as decide:
        output = runner.run(name, "state", params)
    assert output.verdicts == {"verdict": verdict}
    decide.assert_called_once_with("state", {"verdict": question}, purpose=name, harness=None, fallbacks=None)


@pytest.mark.parametrize("params", [[], {"items": "text"}, {"items": None}])
def test_invalid_expansion_parameters_are_refused_before_deciding(definition_home, params):
    raw = sample()
    raw["questions"][0]["each"] = "items"
    write_definition(definition_home, raw)
    with patch.object(runner, "decide") as decide:
        with pytest.raises(DefinitionError) as error:
            runner.run("sample", None, params)
    assert str(error.value) == (
        "classifier parameters must be a mapping" if isinstance(params, list) else "each parameter items must be a list"
    )
    decide.assert_not_called()


def test_expansion_cannot_overwrite_another_question(definition_home):
    raw = sample()
    raw["questions"][0]["each"] = "items"
    raw["questions"].append({"name": "accept_0", "type": "yesno", "instructions": "Other"})
    write_definition(definition_home, raw)
    with patch.object(runner, "decide") as decide:
        with pytest.raises(DefinitionError) as error:
            runner.run("sample", None, {"items": ["one"]})
    assert str(error.value) == "expanded question names must be unique"
    decide.assert_not_called()


def test_missing_format_parameter_has_a_schema_error(definition_home):
    raw = sample()
    raw["questions"][0]["instructions"] = "{missing}"
    write_definition(definition_home, raw)
    with pytest.raises(DefinitionError) as error:
        runner.run("sample", None)
    assert str(error.value) == "cannot format classifier question: 'missing'"


@pytest.mark.parametrize("kind", ["choice", "score"])
@pytest.mark.parametrize("threshold", [None, "floor"])
def test_confidence_rule_handles_absent_confidence_or_no_threshold(definition_home, kind, threshold):
    raw = sample()
    raw["questions"][0].update(type=kind, **({"options": {"a": "Alpha"}} if kind == "choice" else {"levels": ["Low"]}))
    raw["thresholds"] = {"floor": 0}
    raw["rule"] = {"type": kind, "threshold": threshold}
    write_definition(definition_home, raw)
    result = DecisionResult({"accept": Answer(kind, choice="a", score=0.3)}, "stub")
    with patch.object(runner, "decide", return_value=result):
        output = runner.run("sample", "state")
    assert output.verdicts == {"accept": None if threshold else ("a" if kind == "choice" else 0.3)}
