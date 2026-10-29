import json
import re

import pytest

from hooks.classifier import Answer, ClassifierUnavailable, DecisionResult
from scripts import cli_delegates
from scripts.evidence import schema
from scripts.headless.request import READ_ONLY, HeadlessError
from scripts.scout import cli, loop

pytestmark = pytest.mark.unit

QUESTION = "Which Python release added the walrus operator?"
UNTIL = "a primary source names the release"


def _found(source, excerpt, claim="Python 3.8 added assignment expressions"):
    return {"claim": claim, "source": source, "excerpt": excerpt, "retrieved_at": "2026-10-29T05:00:00Z"}


PEP = _found("https://peps.python.org/pep-0572/", "Python-Version: 3.8")
WHATSNEW = _found("https://docs.python.org/3/whatsnew/3.8.html", "Assignment expressions")


class FakeRunner:
    def __init__(self, *rounds, error=None):
        self.rounds = list(rounds)
        self.error = error
        self.calls = []

    def __call__(self, profile_prompt, user_input, schema, clamp, tier, effort, harness="auto"):
        self.calls.append(
            {
                "profile_prompt": profile_prompt,
                "user_input": json.loads(user_input),
                "schema": schema,
                "clamp": clamp,
                "tier": tier,
                "effort": effort,
                "harness": harness,
            }
        )
        if self.error:
            raise self.error
        return {"items": self.rounds[min(len(self.calls), len(self.rounds)) - 1]}


class FakeClassifier:
    def __init__(self, *answers, error=None):
        self.answers = list(answers)
        self.error = error
        self.calls = []

    def __call__(self, state, questions, *, purpose, harness=None, fallbacks=None):
        self.calls.append({"state": state, "questions": questions, "purpose": purpose, "harness": harness})
        if self.error:
            raise self.error
        noul = self.answers[min(len(self.calls), len(self.answers)) - 1]
        return DecisionResult(answers={"met": Answer(type="noul", noul=noul)}, source="fake")


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")

    def install(fake_runner, fake_classifier):
        monkeypatch.setattr(loop.runner, "run", fake_runner)
        monkeypatch.setattr(loop, "decide", fake_classifier)

    return install


def _printed(capsys):
    return json.loads(capsys.readouterr().out)


def test_the_scout_stops_once_the_classifier_says_the_until_condition_holds(wired, capsys):
    runner = FakeRunner([PEP], [WHATSNEW], [_found("https://example.com/late", "never read")])
    classifier = FakeClassifier(0.1, 0.9)
    wired(runner, classifier)

    assert cli.main([QUESTION, "--until", UNTIL, "--rounds", "5"]) == 0

    evidence = schema.validate("evidence", _printed(capsys))
    assert [item["source"] for item in evidence["items"]] == [PEP["source"], WHATSNEW["source"]]
    assert len(runner.calls) == 2
    assert {call["clamp"] for call in runner.calls} == {READ_ONLY}
    assert {call["tier"] for call in runner.calls} == {"low"}
    assert {call["harness"] for call in runner.calls} == {"claude"}
    assert "You answer one question with an evidence set" in runner.calls[0]["profile_prompt"]
    assert not runner.calls[0]["profile_prompt"].startswith("---")
    assert runner.calls[0]["user_input"]["question"] == QUESTION
    assert runner.calls[1]["user_input"]["known"] == [{"source": PEP["source"], "excerpt": PEP["excerpt"]}]
    assert len(classifier.calls) == 2
    asked = classifier.calls[1]["questions"]["met"]
    assert asked.instructions == f"Does this evidence set meet: {UNTIL}?"
    assert classifier.calls[1]["state"]["evidence"]["items"] == evidence["items"]
    assert classifier.calls[0]["purpose"] == "scout-until"


def test_the_round_cap_ends_the_run_when_the_condition_never_holds(wired, capsys):
    runner = FakeRunner([PEP], [WHATSNEW], [_found("https://example.com/third", "third round")])
    classifier = FakeClassifier(0.2)
    wired(runner, classifier)

    assert cli.main([QUESTION, "--until", UNTIL]) == 0

    assert len(runner.calls) == loop.DEFAULT_ROUNDS == 3
    assert len(classifier.calls) == 3
    assert len(_printed(capsys)["items"]) == 3


def test_an_unavailable_classifier_keeps_the_scout_going_to_the_round_cap(wired, capsys):
    runner = FakeRunner([PEP], [WHATSNEW])
    wired(runner, FakeClassifier(error=ClassifierUnavailable("no decision backend answered")))

    assert cli.main([QUESTION, "--until", UNTIL, "--rounds", "2"]) == 0

    assert len(runner.calls) == 2
    assert len(_printed(capsys)["items"]) == 2


def test_the_classifier_is_asked_after_a_round_that_found_nothing(wired, capsys):
    runner = FakeRunner([], [PEP])
    classifier = FakeClassifier(0.0, 1.0)
    wired(runner, classifier)

    assert cli.main([QUESTION, "--until", UNTIL, "--rounds", "3"]) == 0

    assert len(runner.calls) == 2
    assert classifier.calls[0]["state"]["evidence"]["items"] == []
    assert len(classifier.calls) == 2


def test_without_an_until_condition_the_classifier_is_never_asked(wired, capsys):
    runner = FakeRunner([PEP])
    classifier = FakeClassifier(1.0)
    wired(runner, classifier)

    assert cli.main([QUESTION, "--rounds", "2"]) == 0

    assert len(runner.calls) == 2
    assert classifier.calls == []


def test_duplicate_items_are_merged_by_source_and_excerpt(wired, capsys):
    reworded = {**PEP, "claim": "The walrus operator arrived in 3.8", "retrieved_at": "later"}
    runner = FakeRunner([PEP, PEP], [reworded, WHATSNEW])
    wired(runner, FakeClassifier(0.0))

    assert cli.main([QUESTION, "--rounds", "2"]) == 0

    items = schema.validate("evidence", _printed(capsys))["items"]
    assert [(item["source"], item["claim"]) for item in items] == [
        (PEP["source"], PEP["claim"]),
        (WHATSNEW["source"], WHATSNEW["claim"]),
    ]
    assert [item["id"] for item in items] == ["e1", "e2"]


def test_the_out_file_receives_the_evidence_set(wired, tmp_path, capsys):
    wired(FakeRunner([PEP]), FakeClassifier(1.0))
    out = tmp_path / "evidence.json"

    assert cli.main([QUESTION, "--until", UNTIL, "--out", str(out), "--tier", "high", "--harness", "codex"]) == 0

    assert schema.load_evidence(out) == _printed(capsys)


def test_the_requested_tier_and_harness_reach_every_round(wired, capsys):
    runner = FakeRunner([PEP])
    wired(runner, FakeClassifier(1.0))

    assert cli.main([QUESTION, "--until", UNTIL, "--tier", "high", "--harness", "codex"]) == 0

    assert runner.calls[0]["tier"] == "high"
    assert runner.calls[0]["harness"] == "codex"


def test_no_evidence_after_every_round_exits_nonzero(wired, capsys):
    wired(FakeRunner([]), FakeClassifier(0.0))

    assert cli.main([QUESTION, "--rounds", "2"]) == 1

    assert "no source supported an answer" in capsys.readouterr().err


def test_a_headless_failure_exits_nonzero(wired, capsys):
    wired(FakeRunner(error=HeadlessError("claude exited 1")), FakeClassifier(1.0))

    assert cli.main([QUESTION]) == 1

    assert "claude exited 1" in capsys.readouterr().err


def test_a_round_cap_below_one_is_refused(wired):
    wired(FakeRunner([PEP]), FakeClassifier(1.0))

    with pytest.raises(SystemExit):
        cli.main([QUESTION, "--rounds", "0"])


def test_each_round_sends_the_exact_round_request_and_until_question(wired, capsys):
    runner = FakeRunner([PEP])
    classifier = FakeClassifier(0.5)
    wired(runner, classifier)

    assert cli.main([QUESTION, "--until", UNTIL, "--rounds", "1"]) == 0

    out = capsys.readouterr().out
    evidence = json.loads(out)
    assert out == json.dumps(evidence, indent=2) + "\n"
    call = runner.calls[0]
    assert call["user_input"] == {"question": QUESTION, "until": UNTIL, "rounds": 1, "known": []}
    assert call["effort"] == "low"
    assert call["schema"]["properties"]["items"]["items"]["required"] == ["claim", "source", "excerpt", "retrieved_at"]
    asked = classifier.calls[0]
    assert asked["questions"]["met"].criteria() == {
        "true": "The evidence set meets the condition",
        "false": "The evidence set does not meet the condition yet",
    }
    assert asked["state"] == {"question": QUESTION, "until": UNTIL, "evidence": evidence}
    assert asked["harness"] == "claude"


def test_a_classifier_answer_at_the_threshold_ends_the_run(wired, capsys):
    runner = FakeRunner([PEP], [WHATSNEW])
    wired(runner, FakeClassifier(loop.MET))

    assert cli.main([QUESTION, "--until", UNTIL, "--rounds", "2"]) == 0

    assert len(runner.calls) == 1


def test_the_scouter_prompt_is_the_body_after_the_frontmatter(monkeypatch, tmp_path):
    agent = tmp_path / "scouter.md"
    agent.write_text("---\nname: scouter\n---\n\nFind sources.\n\n---\n\nQuote them exactly.\n")
    monkeypatch.setattr(loop, "SCOUTER", agent)

    assert loop.scouter_prompt() == "Find sources.\n\n---\n\nQuote them exactly."


def test_one_round_is_the_smallest_cap(wired, capsys):
    runner = FakeRunner([PEP])
    wired(runner, FakeClassifier(0.0))

    assert cli.main([QUESTION, "--rounds", "1"]) == 0

    assert len(runner.calls) == 1


def test_a_round_cap_below_one_names_the_limit(capsys):
    with pytest.raises(SystemExit):
        cli.main([QUESTION, "--rounds", "0"])

    assert "argument --rounds: rounds must be at least 1\n" in capsys.readouterr().err


def test_help_names_the_command_its_flags_and_choices(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])

    text = " ".join(capsys.readouterr().out.split())
    for expected in (
        "usage: agentihooks scout",
        "Answer a question with an evidence set",
        "condition the evidence set must meet",
        "file the evidence set is written to",
        "--tier {low,high}",
        "--harness {auto,claude,codex}",
    ):
        assert re.search(rf"(?<![A-Za-z]){re.escape(expected)}(?![A-Za-z])", text), expected


def test_scout_is_a_delegated_subcommand():
    assert cli_delegates.delegated_cli(["scout"]) is cli.main
