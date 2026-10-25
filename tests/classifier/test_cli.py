import json
import re

import pytest

from hooks.classifier import api, cli, down_cache
from hooks.classifier.questions import Choice, Score, YesNo, questions_from_wire
from hooks.classifier.settings import DEFAULT_MODELS
from tests.classifier.fakes import KEY, FakeUrlopen, http_error, ok

WIRE = {
    "tier": {"type": "choice", "instructions": "Which tier?", "criteria": {"small": "s", "large": "l"}},
    "effort": {"type": "score", "instructions": "How much?", "criteria": ["low", "high"]},
    "trivial": {"type": "noul", "instructions": "One line?", "criteria": {"true": "t", "false": "f"}},
}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_URL", "http://litellm:4000")
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_LITELLM_KEY", KEY)
    monkeypatch.delenv("AGENTIHOOKS_CLASSIFIER_MODELS", raising=False)


@pytest.fixture
def files(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"task": "typo"}))
    questions = tmp_path / "questions.json"
    questions.write_text(json.dumps(WIRE))
    return state, questions


def test_questions_from_wire_builds_typed_questions():
    built = questions_from_wire(WIRE)
    assert built == {
        "tier": Choice("Which tier?", {"small": "s", "large": "l"}),
        "effort": Score("How much?", ["low", "high"]),
        "trivial": YesNo("One line?", true="t", false="f"),
    }


@pytest.mark.parametrize(
    "bad",
    [{"x": {"type": "rank", "instructions": "q", "criteria": []}}, {"x": {"type": "noul"}}, {"x": "noul"}, []],
)
def test_malformed_wire_questions_are_input_errors(bad):
    from hooks.classifier import ClassifierInputError

    with pytest.raises(ClassifierInputError):
        questions_from_wire(bad)


def test_classify_prints_the_result_json(monkeypatch, files, capsys):
    fake = FakeUrlopen({m: ok() for m in DEFAULT_MODELS})
    monkeypatch.setattr(api, "urlopen", fake)
    state, questions = files
    assert cli.classify_main(["--state", str(state), "--questions", str(questions), "--purpose", "smoke"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["source"] == "liquid-d1"
    assert out["calibrated"] is True
    assert out["answers"]["tier"]["choice"] == "small"
    assert out["answers"]["trivial"] == {"type": "noul", "noul": 0.74}
    assert fake.calls[0]["body"]["state"] == {"task": "typo"}


def test_classify_sends_a_plain_text_state_as_a_string(monkeypatch, files, capsys):
    fake = FakeUrlopen({m: ok() for m in DEFAULT_MODELS})
    monkeypatch.setattr(api, "urlopen", fake)
    state, questions = files
    state.write_text("fix the typo in the README\n")
    assert cli.classify_main(["--state", str(state), "--questions", str(questions)]) == 0
    assert fake.calls[0]["body"]["state"] == "fix the typo in the README\n"


def test_classify_input_error_exits_2(files, capsys):
    state, questions = files
    questions.write_text(json.dumps({"Bad": WIRE["trivial"]}))
    assert cli.classify_main(["--state", str(state), "--questions", str(questions)]) == 2
    assert "name" in capsys.readouterr().err


def test_classify_caller_bug_exits_2(monkeypatch, files, capsys):
    monkeypatch.setattr(api, "urlopen", FakeUrlopen({"liquid-d1": http_error(400, "invalid_union")}))
    state, questions = files
    assert cli.classify_main(["--state", str(state), "--questions", str(questions)]) == 2
    assert "invalid_union" in capsys.readouterr().err


def test_classify_unavailable_exits_1(monkeypatch, files, capsys):
    monkeypatch.setattr(api, "urlopen", FakeUrlopen({m: http_error(503) for m in DEFAULT_MODELS}))
    state, questions = files
    assert cli.classify_main(["--state", str(state), "--questions", str(questions)]) == 1
    assert "no decision backend answered" in capsys.readouterr().err
    assert down_cache.is_down(120)


def test_stats_counts_sources_fallback_rate_and_latency(monkeypatch, files, capsys):
    from hooks.classifier import decision_log
    from hooks.classifier.result import Answer, DecisionResult

    api_result = DecisionResult({"a": Answer("noul", noul=0.9)}, source="liquid-d1", cost=0.5)
    fallback_result = DecisionResult({"a": Answer("noul", noul=0.6)}, source="haiku", calibrated=False)
    for latency in (100, 300, 200):
        decision_log.append("model-pick", "s", api_result, latency)
    decision_log.append("model-pick", "s", fallback_result, 4000)
    decision_log.append("model-pick", "s", None, 50)
    decision_log.append("gate", "s", api_result, 10)

    assert cli.classifier_main(["stats", "--purpose", "model-pick"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {
        "purpose": "model-pick",
        "calls": 5,
        "unavailable": 1,
        "sources": {"liquid-d1": 3, "haiku": 1},
        "fallback_rate": 0.25,
        "latency_ms": {"p50": 200, "p90": 4000, "p99": 4000},
        "cost": 1.5,
    }
    assert cli.classifier_main(["stats"]) == 0
    everything = json.loads(capsys.readouterr().out)
    assert (everything["calls"], everything["purpose"]) == (6, None)
    assert everything["latency_ms"] == {"p50": 100, "p90": 4000, "p99": 4000}


def test_stats_on_an_empty_log(capsys):
    assert cli.classifier_main(["stats"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["calls"] == 0 and out["fallback_rate"] is None
    assert out["latency_ms"] == {"p50": None, "p90": None, "p99": None}


def test_install_dispatches_both_commands(monkeypatch):
    from scripts import install

    seen = []
    monkeypatch.setattr(cli, "classify_main", lambda argv: seen.append(("classify", argv)) or 0)
    monkeypatch.setattr(cli, "classifier_main", lambda argv: seen.append(("classifier", argv)) or 0)
    for argv in (["classify", "--state", "s"], ["classifier", "stats"]):
        monkeypatch.setattr("sys.argv", ["agentihooks", *argv])
        with pytest.raises(SystemExit) as done:
            install.main()
        assert done.value.code == 0
    assert seen == [("classify", ["--state", "s"]), ("classifier", ["stats"])]


def test_agentihooks_help_lists_both_commands(monkeypatch, capsys):
    from scripts import install

    monkeypatch.setattr("sys.argv", ["agentihooks", "--help"])
    with pytest.raises(SystemExit) as done:
        install.main()
    assert done.value.code == 0
    out = " ".join(capsys.readouterr().out.split())
    for text in (
        "classify Ask the decision models typed questions: --state FILE --questions FILE",
        "classifier Decision classifier records: stats [--purpose P], eval NAME [--live N]",
    ):
        assert re.search(rf"(^|\s){re.escape(text)}($|\s)", out), text


@pytest.mark.parametrize(
    ("main", "argv", "expected"),
    [
        (
            "classify_main",
            ["--help"],
            [
                "agentihooks classify",
                "Ask the decision models typed questions",
                "File holding the state: JSON, or plain text",
                "JSON file of named questions: type, instructions, criteria",
                "Caller name recorded in the decision log",
            ],
        ),
        (
            "classifier_main",
            ["--help"],
            [
                "agentihooks classifier",
                "Decision classifier records",
                "Counts, sources, fallback rate and latency from the decision log",
            ],
        ),
        ("classifier_main", ["stats", "--help"], ["agentihooks classifier stats", "Only calls made for this purpose"]),
    ],
)
def test_help_text(capsys, main, argv, expected):
    with pytest.raises(SystemExit) as done:
        getattr(cli, main)(argv)
    assert done.value.code == 0
    out = " ".join(capsys.readouterr().out.split())
    for text in expected:
        assert re.search(rf"(^|[\s\[]){re.escape(text)}($|[\s\]:])", out), text


@pytest.mark.parametrize("missing", ["--state", "--questions"])
def test_classify_requires_state_and_questions(files, missing):
    state, questions = files
    argv = {"--state": str(state), "--questions": str(questions)}
    argv.pop(missing)
    with pytest.raises(SystemExit) as done:
        cli.classify_main([part for pair in argv.items() for part in pair])
    assert done.value.code == 2


def test_classifier_needs_a_subcommand(capsys):
    with pytest.raises(SystemExit) as done:
        cli.classifier_main([])
    assert done.value.code == 2
    assert "the following arguments are required: command" in capsys.readouterr().err


def test_classify_records_its_purpose_and_prints_indented_json(monkeypatch, files, capsys):
    from hooks.classifier import decision_log

    monkeypatch.setattr(api, "urlopen", FakeUrlopen({m: ok() for m in DEFAULT_MODELS}))
    state, questions = files
    cli.classify_main(["--state", str(state), "--questions", str(questions)])
    cli.classify_main(["--state", str(state), "--questions", str(questions), "--purpose", "smoke"])
    assert [e["purpose"] for e in decision_log.read()] == ["cli", "smoke"]
    assert capsys.readouterr().out.startswith('{\n  "source"')


def test_stats_prints_indented_json(capsys):
    cli.classifier_main(["stats"])
    assert capsys.readouterr().out.startswith('{\n  "purpose"')
