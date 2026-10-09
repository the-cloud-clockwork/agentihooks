from unittest.mock import patch

import pytest

from hooks.classifier.questions import YesNo
from hooks.classifier.result import Answer, DecisionResult

from .test_definitions import definition_home as definition_home
from .test_definitions import sample, write_definition


@pytest.mark.parametrize(
    ("kind", "question", "answer", "threshold", "verdict"),
    [
        ("yes", {"type": "yesno"}, Answer("noul", noul=0.7), 0.6, True),
        ("yes", {"type": "yesno"}, Answer("noul", noul=0.5), 0.6, False),
        (
            "choice",
            {"type": "choice", "options": {"a": "Alpha", "b": "Beta"}},
            Answer("choice", choice="b", confidence=0.7),
            0.6,
            "b",
        ),
        (
            "choice",
            {"type": "choice", "options": {"a": "Alpha", "b": "Beta"}},
            Answer("choice", choice="b", confidence=0.5),
            0.6,
            None,
        ),
        ("score", {"type": "score", "levels": ["low", "high"]}, Answer("score", score=0.8, confidence=0.7), 0.6, 0.8),
        ("score", {"type": "score", "levels": ["low", "high"]}, Answer("score", score=0.8, confidence=0.5), 0.6, None),
        ("code", {"type": "yesno"}, Answer("noul", noul=0.7), 0.6, None),
    ],
)
def test_runner_applies_defined_verdict_rule(definition_home, kind, question, answer, threshold, verdict):
    from hooks.classifier import runner

    raw = sample()
    raw["questions"][0].update(question)
    raw["thresholds"] = {"floor": threshold}
    raw["rule"] = {"type": kind, "threshold": "floor"}
    write_definition(definition_home, raw)
    result = DecisionResult({"accept": answer}, "stub")
    with patch.object(runner, "decide", return_value=result) as decide:
        output = runner.run("sample", {"value": 1}, harness="codex")
    assert output.verdicts == ({} if kind == "code" else {"accept": verdict})
    assert output.raw is result
    assert output.definition.name == "sample"
    assert output.thresholds == {"floor": threshold}
    decide.assert_called_once_with(
        {"value": 1},
        {"accept": output.definition.questions[0].question},
        purpose="sample",
        harness="codex",
        fallbacks=[],
    )


def test_runner_expands_each_and_formats_question_text(definition_home):
    from hooks.classifier import runner

    raw = sample()
    raw["fallbacks"] = "cli"
    raw["questions"] = [
        {
            "name": "accept",
            "type": "yesno",
            "instructions": "{prefix}: {item[title]} at {index}",
            "true": "Keep {item[title]}",
            "false": "Drop {item[title]}",
            "each": "items",
        }
    ]
    write_definition(definition_home, raw)
    result = DecisionResult({"accept_0": Answer("noul", noul=0.7), "accept_1": Answer("noul", noul=0.2)}, "stub")
    with patch.object(runner, "decide", return_value=result) as decide:
        output = runner.run("sample", "state", {"prefix": "Task", "items": [{"title": "One"}, {"title": "Two"}]})
    assert output.verdicts == {"accept_0": True, "accept_1": False}
    decide.assert_called_once_with(
        "state",
        {
            "accept_0": YesNo("Task: One at 0", "Keep One", "Drop One"),
            "accept_1": YesNo("Task: Two at 1", "Keep Two", "Drop Two"),
        },
        purpose="sample",
        harness=None,
        fallbacks=None,
    )
