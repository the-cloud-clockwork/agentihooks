import pytest

from hooks.classifier import runner
from hooks.classifier.result import Answer, DecisionResult

from .test_definitions import definition_home as definition_home
from .test_definitions import sample, write_definition


def test_runner_uses_injected_decision_and_environment(definition_home, monkeypatch):
    write_definition(definition_home, sample())
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_SAMPLE_YES", "0.1")
    result = DecisionResult({"accept": Answer("noul", noul=0.7)}, "recorded")
    calls = []

    def judge(state, questions, **kwargs):
        calls.append((state, questions, kwargs))
        return result

    output = runner.run(
        "sample",
        {"value": 1},
        decider=judge,
        environ={"AGENTIHOOKS_CLASSIFIER_SAMPLE_YES": "0.8"},
    )

    assert output.raw is result
    assert output.verdicts == {"accept": False}
    assert output.thresholds == {"yes": 0.8}
    assert calls == [
        (
            {"value": 1},
            {"accept": output.definition.questions[0].question},
            {"purpose": "sample", "fallbacks": []},
        )
    ]


@pytest.mark.parametrize(
    ("fallbacks", "harness", "options"),
    [
        ("cli", None, {"purpose": "sample"}),
        ("cli", "codex", {"purpose": "sample", "harness": "codex"}),
        ("none", "claude", {"purpose": "sample", "harness": "claude", "fallbacks": []}),
    ],
)
def test_injected_decider_receives_only_options_that_differ_from_decide_defaults(
    definition_home, fallbacks, harness, options
):
    raw = sample()
    raw["fallbacks"] = fallbacks
    write_definition(definition_home, raw)
    calls = []

    def judge(state, questions, **kwargs):
        calls.append(kwargs)
        return DecisionResult({"accept": Answer("noul", noul=0.7)}, "recorded")

    runner.run("sample", {"value": 1}, harness=harness, decider=judge, environ={})
    assert calls == [options]
