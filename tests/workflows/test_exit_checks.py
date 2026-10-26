import json

import pytest

from scripts.headless import runner
from scripts.workflows import exit_checks
from scripts.workflows.schema import WorkflowError


def recorder(*replies):
    calls = []

    def classify(expression, outputs_text):
        calls.append((expression, outputs_text))
        return replies[len(calls) - 1]

    classify.calls = calls
    return classify


@pytest.mark.parametrize(
    ("expression", "outputs", "expected"),
    [
        ("scout.pass", {"scout": {"verdict": "pass"}}, True),
        ("scout.pass", {"scout": {"verdict": "retry"}}, False),
        ("scout.pass", {}, False),
        ("scout.pass", {"scout": "pass"}, False),
        ("scout.pass", {"scout": {}}, False),
        ("scout.pass", None, False),
        ("scout.pass", ["scout"], False),
        ("pass", {"verdict": "pass"}, True),
        ("retry", {"verdict": "retry"}, True),
        ("enhance", {"verdict": "enhance"}, True),
        ("pass", {"verdict": "retry"}, False),
        ("pass", {}, False),
        ("pass", None, False),
        ("pass", ["x"], False),
    ],
)
def test_deterministic_forms_never_call_the_classifier(expression, outputs, expected):
    classify = recorder()

    assert exit_checks.check(expression, outputs, classify) is expected
    assert classify.calls == []


def test_free_text_is_routed_to_the_classifier_with_the_sorted_json_outputs():
    classify = recorder(True)

    assert exit_checks.check("every point of the case has cited evidence", {"b": 2, "a": 1}, classify) is True
    assert classify.calls == [
        ("every point of the case has cited evidence", json.dumps({"a": 1, "b": 2}, sort_keys=True))
    ]


def test_free_text_outputs_that_do_not_serialize_fall_back_to_str():
    class Odd:
        def __str__(self):
            return "odd"

    classify = recorder(False)

    assert exit_checks.check("whatever this means", {"thing": Odd()}, classify) is False
    assert classify.calls == [("whatever this means", json.dumps({"thing": "odd"}, sort_keys=True))]


def test_classify_with_headless_builds_the_exact_runner_call(monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "run", lambda *a: calls.append(a) or {"satisfied": False})

    assert exit_checks.classify_with_headless("cond", "OUT") is False
    assert calls == [
        (
            exit_checks.CLASSIFIER_PROFILE,
            "condition: cond\noutputs: OUT",
            exit_checks.CLASSIFIER_SCHEMA,
            "sealed",
            "low",
            "low",
        )
    ]


def test_check_falls_back_to_the_default_classifier_when_none_is_given(monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "run", lambda *a: calls.append(a) or {"satisfied": True})

    assert exit_checks.check("the change is done", {"build": {"done": True}}) is True
    assert calls == [
        (
            exit_checks.CLASSIFIER_PROFILE,
            'condition: the change is done\noutputs: {"build": {"done": true}}',
            exit_checks.CLASSIFIER_SCHEMA,
            "sealed",
            "low",
            "low",
        )
    ]


@pytest.mark.parametrize("expression", ["", "   "])
def test_an_empty_expression_is_refused(expression):
    with pytest.raises(WorkflowError, match="must not be empty"):
        exit_checks.check(expression, {}, recorder())
