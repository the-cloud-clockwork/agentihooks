import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import yaml

from hooks.classifier import cli, corpus, evaluation
from hooks.classifier.errors import BackendFailure
from hooks.classifier.result import Answer, DecisionResult
from scripts.swarm import metrics_outbox

from .test_definitions import definition_home as definition_home
from .test_definitions import sample, write_definition

PACKAGE = Path(__file__).resolve().parents[2] / "profiles" / "package" / "classifiers"
ON = {"AGENTIHOOKS_METRICS_URL": "http://ch:8123", "AGENTIHOOKS_METRICS_USER": "writer", "AGENTIHOOKS_SWARM": "sw"}


def noul(value, source="liquid-d1", latency=40):
    return {"source": source, "latency_ms": latency, "answers": {"accept": {"type": "noul", "noul": value}}}


def case(name, expected, *samples, control=False):
    return {
        "name": name,
        "state": {"task": name},
        "expected": {"accept": expected},
        "control": control,
        "samples": list(samples),
    }


def write_corpus(folder, cases, name="sample"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.corpus.yaml").write_text(yaml.safe_dump({"version": 1, "cases": cases}))


@pytest.fixture
def home(definition_home):
    write_definition(definition_home, sample())
    return definition_home


def test_replay_scores_a_planted_wrong_sample_as_a_miss(home):
    write_corpus(home, [case("typo", True, noul(0.9), noul(0.2))])
    report = evaluation.evaluate("sample").report()
    assert report["mode"] == "replay"
    assert report["samples"] == 2
    assert report["wrong"] == 1
    assert report["wrong_cases"] == ["typo"]
    assert report["held_controls"] == []
    assert report["backends"]["liquid-d1"] == {
        "samples": 2,
        "wrong": 1,
        "failures": 0,
        "wrong_cases": ["typo"],
        "held_controls": [],
        "latency_ms": {"p50": 40, "max": 40},
    }


def test_replay_scores_a_rejected_control_as_held(home):
    write_corpus(
        home,
        [
            case("unrelated", False, noul(0.1, latency=30), noul(0.3, source="haiku", latency=900), control=True),
            case("typo", True, noul(0.9, latency=50)),
        ],
    )
    report = evaluation.evaluate("sample").report()
    assert report["cases"] == 2
    assert report["controls"] == 1
    assert report["wrong"] == 0
    assert report["wrong_cases"] == []
    assert report["held_controls"] == ["unrelated"]
    assert report["backends"]["haiku"]["held_controls"] == ["unrelated"]
    assert report["backends"]["liquid-d1"]["held_controls"] == ["unrelated"]
    assert report["backends"]["liquid-d1"]["latency_ms"] == {"p50": 30, "max": 50}


def test_replay_does_not_hold_a_control_one_sample_accepts(home):
    write_corpus(home, [case("unrelated", False, noul(0.1), noul(0.7, source="haiku"), control=True)])
    report = evaluation.evaluate("sample").report()
    assert report["held_controls"] == []
    assert report["wrong_cases"] == ["unrelated"]
    assert report["backends"]["liquid-d1"]["held_controls"] == ["unrelated"]
    assert report["backends"]["haiku"]["held_controls"] == []


def test_replay_applies_the_choice_rule_threshold(definition_home):
    raw = sample()
    raw["questions"] = [{"name": "accept", "type": "choice", "instructions": "Which?", "options": {"a": "A", "b": "B"}}]
    raw["rule"] = {"type": "choice", "threshold": "yes"}
    write_definition(definition_home, raw)

    def pick(choice, confidence):
        answer = {"type": "choice", "choice": choice, "confidence": confidence}
        return {"source": "jev-1.13", "latency_ms": 10, "answers": {"accept": answer}}

    write_corpus(
        definition_home,
        [case("sure", "b", pick("b", 0.9)), case("unsure", None, pick("a", 0.4), control=True)],
    )
    report = evaluation.evaluate("sample").report()
    assert report["wrong_cases"] == []
    assert report["held_controls"] == ["unsure"]


def test_replay_scores_a_score_verdict_against_its_expected_range(definition_home):
    raw = sample()
    raw["questions"] = [{"name": "accept", "type": "score", "instructions": "How hard?", "levels": ["low", "high"]}]
    raw["rule"] = {"type": "score", "threshold": "yes"}
    write_definition(definition_home, raw)

    def rate(score, confidence=0.9):
        answer = {"type": "score", "score": score, "confidence": confidence}
        return {"source": "pplx-decider-v1-27b", "latency_ms": 10, "answers": {"accept": answer}}

    write_corpus(
        definition_home,
        [
            case("easy", [0.0, 0.2], rate(0), rate(0.2)),
            case("hard", [0.7, 1], rate(0.69), rate(0.9, confidence=0.3)),
            case("unsure", None, rate(0.9, confidence=0.59), control=True),
        ],
    )
    report = evaluation.evaluate("sample").report()
    assert report["wrong"] == 2
    assert report["wrong_cases"] == ["hard"]
    assert report["held_controls"] == ["unsure"]


@pytest.mark.parametrize("bad", [0.5, [0.5], [0.6, 0.4], [-0.1, 0.5], [0.5, 1.1], [True, 1], "low"])
def test_load_refuses_a_score_expectation_that_is_not_a_range(definition_home, bad):
    raw = sample()
    raw["questions"] = [{"name": "accept", "type": "score", "instructions": "How hard?", "levels": ["low", "high"]}]
    raw["rule"] = {"type": "score", "threshold": "yes"}
    write_definition(definition_home, raw)
    answer = {"type": "score", "score": 0.5, "confidence": 0.9}
    write_corpus(definition_home, [case("easy", bad, {"source": "m", "latency_ms": 1, "answers": {"accept": answer}})])
    with pytest.raises(corpus.CorpusError) as error:
        evaluation.evaluate("sample")
    assert str(error.value) == "case easy expected accept is not a verdict its rule can give"


@pytest.mark.parametrize(
    ("cases", "message"),
    [
        ([case("typo", True)], "case typo needs at least one recorded sample"),
        (
            [case("typo", True, {**noul(0.9), "answers": {"other": {"type": "noul", "noul": 0.9}}})],
            "case typo sample 0 answers must name exactly: accept",
        ),
        (
            [case("typo", True, {**noul(0.9), "answers": {"accept": {"type": "score", "score": 0.9}}})],
            "case typo sample 0 answer accept must be a noul answer",
        ),
        (
            [{**case("typo", True, noul(0.9)), "expected": {"other": True}}],
            "case typo expected must name exactly: accept",
        ),
        ([case("typo", True, noul(0.9), control=True)], "control case typo must expect only rejections"),
        ([case("typo", True, noul(0.9)), case("typo", True, noul(0.9))], "case names must be unique"),
        ([{**case("typo", True, noul(0.9)), "extra": 1}], "unknown case keys: extra"),
        (["typo"], "case must be a mapping"),
        ([{**case("typo", True, noul(0.9)), "name": ""}], "case name must be nonempty text"),
        ([{k: v for k, v in case("typo", True, noul(0.9)).items() if k != "state"}], "case typo needs a state"),
        ([{**case("typo", True, noul(0.9)), "params": [1]}], "case typo params must be a mapping"),
        ([{**case("typo", True, noul(0.9)), "control": "yes"}], "case typo control must be true or false"),
        ([case("typo", "yes", noul(0.9))], "case typo expected accept is not a verdict its rule can give"),
        ([case("typo", None, noul(0.9))], "case typo expected accept is not a verdict its rule can give"),
        ([case("typo", True, "sample")], "case typo sample 0 must be a mapping"),
        ([case("typo", True, {**noul(0.9), "extra": 1})], "unknown case typo sample 0 keys: extra"),
        ([case("typo", True, noul(0.9, source=""))], "case typo sample 0 needs a source"),
        (
            [case("typo", True, noul(0.9, latency=-1))],
            "case typo sample 0 latency_ms must be a whole number of milliseconds",
        ),
        (
            [case("typo", True, noul(0.9, latency=1.5))],
            "case typo sample 0 latency_ms must be a whole number of milliseconds",
        ),
        (
            [case("typo", True, {**noul(0.9), "answers": {"accept": {"type": "noul"}}})],
            "case typo sample 0 answer accept must be a noul answer",
        ),
    ],
)
def test_load_refuses_a_malformed_corpus(home, cases, message):
    write_corpus(home, cases)
    with pytest.raises(corpus.CorpusError) as error:
        evaluation.evaluate("sample")
    assert str(error.value) == message


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("- 1\n", "corpus must be a mapping"),
        ("version: 2\ncases: [1]\n", "corpus version must be 1"),
        ("version: true\ncases: [1]\n", "corpus version must be 1"),
        ("version: 1\ncases: []\n", "corpus cases must be a nonempty list"),
        ("version: 1\n", "corpus cases must be a nonempty list"),
        ("version: 1\ncases: [1]\nextra: 1\n", "unknown corpus keys: extra"),
    ],
)
def test_load_refuses_a_malformed_corpus_document(home, text, message):
    (home / "sample.corpus.yaml").write_text(text)
    with pytest.raises(corpus.CorpusError) as error:
        evaluation.evaluate("sample")
    assert str(error.value) == message


def test_load_refuses_an_unreadable_corpus(home):
    (home / "sample.corpus.yaml").write_text("cases: [\n")
    with pytest.raises(corpus.CorpusError) as error:
        evaluation.evaluate("sample")
    assert str(error.value).startswith("cannot read corpus sample: ")


@pytest.mark.parametrize(
    ("threshold", "expected", "message"),
    [
        ("yes", "c", "case typo expected accept is not a verdict its rule can give"),
        (None, None, "case typo expected accept is not a verdict its rule can give"),
        (None, "b", None),
    ],
)
def test_load_checks_a_choice_expectation_against_its_options(definition_home, threshold, expected, message):
    raw = sample()
    raw["questions"] = [{"name": "accept", "type": "choice", "instructions": "Which?", "options": {"a": "A", "b": "B"}}]
    raw["rule"] = {"type": "choice"} if threshold is None else {"type": "choice", "threshold": threshold}
    write_definition(definition_home, raw)
    answer = {"type": "choice", "choice": "b", "confidence": 0.9}
    write_corpus(
        definition_home, [case("typo", expected, {"source": "m", "latency_ms": 1, "answers": {"accept": answer}})]
    )
    if message is None:
        assert evaluation.evaluate("sample").report()["wrong"] == 0
        return
    with pytest.raises(corpus.CorpusError) as error:
        evaluation.evaluate("sample")
    assert str(error.value) == message


def test_load_refuses_a_missing_corpus(home):
    with pytest.raises(corpus.CorpusError) as error:
        evaluation.evaluate("sample")
    assert str(error.value) == f"no corpus for classifier sample: {home / 'sample.corpus.yaml'}"


def test_corpus_sits_next_to_the_selected_definition(home, tmp_path):
    assert corpus.path_for("sample") == home / "sample.corpus.yaml"
    runtime = tmp_path / "home" / "classifiers"
    write_definition(runtime, sample())
    assert corpus.path_for("sample") == runtime / "sample.corpus.yaml"


def test_code_rule_definitions_cannot_be_replayed(definition_home):
    raw = sample()
    raw["rule"] = {"type": "code"}
    write_definition(definition_home, raw)
    write_corpus(definition_home, [case("typo", True, noul(0.9))])
    with pytest.raises(corpus.CorpusError) as error:
        evaluation.evaluate("sample")
    assert str(error.value) == "classifier sample keeps a code rule; replay needs a yes, choice or score rule"


class StubBackend:
    def __init__(self, name, noul_value=None):
        self.name = name
        self.noul_value = noul_value
        self.calls = []

    def decide(self, request):
        self.calls.append(request)
        if self.noul_value is None:
            raise BackendFailure(f"{self.name}: timeout")
        return DecisionResult({"accept": Answer("noul", noul=self.noul_value)}, self.name)


@pytest.mark.parametrize("value", ["true", ""])
def test_live_runs_are_refused_in_ci(home, monkeypatch, value):
    monkeypatch.setenv("CI", value)
    write_corpus(home, [case("typo", True, noul(0.9))])
    backend = StubBackend("haiku", 0.9)
    with pytest.raises(corpus.CorpusError) as error:
        evaluation.evaluate("sample", repeats=1, backends=[backend])
    assert str(error.value) == "live classifier runs are refused in CI"
    assert backend.calls == []


def test_live_asks_every_backend_repeats_times_per_case(home, monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    write_corpus(home, [case("typo", True, noul(0.9)), case("unrelated", False, noul(0.1), control=True)])
    good, wrong, down = StubBackend("liquid-d1", 0.9), StubBackend("luna", 0.1), StubBackend("haiku")
    report = evaluation.evaluate("sample", repeats=2, backends=[good, wrong, down]).report()
    assert [len(b.calls) for b in (good, wrong, down)] == [4, 4, 4]
    assert good.calls[0].state == {"task": "typo"}
    assert set(good.calls[0].questions) == {"accept"}
    assert report["mode"] == "live"
    assert report["backends"]["liquid-d1"]["wrong_cases"] == ["unrelated"]
    assert report["backends"]["luna"]["wrong_cases"] == ["typo"]
    assert report["backends"]["luna"]["held_controls"] == ["unrelated"]
    assert report["backends"]["haiku"]["failures"] == 4
    assert report["backends"]["haiku"]["samples"] == 0
    assert report["backends"]["haiku"]["latency_ms"] == {"p50": None, "max": None}
    assert report["held_controls"] == []
    assert report["wrong_cases"] == ["typo", "unrelated"]


def test_live_backends_are_every_api_model_then_haiku_and_luna(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_URL", "http://litellm:4000")
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_LITELLM_KEY", "test-key")
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_MODELS", "m1,m2")
    assert [backend.name for backend in evaluation.live_backends()] == ["m1", "m2", "haiku", "luna"]
    monkeypatch.delenv("AGENTIHOOKS_CLASSIFIER_URL")
    assert [backend.name for backend in evaluation.live_backends()] == ["haiku", "luna"]


def test_eval_command_prints_the_report_and_fails_on_a_miss(home, capsys):
    write_corpus(home, [case("typo", True, noul(0.2))])
    assert cli.classifier_main(["eval", "sample"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["classifier"] == "sample"
    assert report["wrong_cases"] == ["typo"]


def test_eval_command_passes_a_clean_corpus_and_refuses_a_bad_one(home, capsys):
    write_corpus(home, [case("typo", True, noul(0.9))])
    assert cli.classifier_main(["eval", "sample"]) == 0
    assert json.loads(capsys.readouterr().out)["wrong"] == 0
    write_corpus(home, [case("typo", True)])
    assert cli.classifier_main(["eval", "sample"]) == 2
    assert capsys.readouterr().err == "classifier eval: case typo needs at least one recorded sample\n"


def test_eval_command_live_flag_reaches_the_backends(home, monkeypatch, capsys):
    monkeypatch.delenv("CI", raising=False)
    write_corpus(home, [case("typo", True, noul(0.9))])
    backend = StubBackend("haiku", 0.9)
    monkeypatch.setattr(evaluation, "live_backends", lambda: [backend])
    assert cli.classifier_main(["eval", "sample", "--live", "3"]) == 0
    assert len(backend.calls) == 3
    assert json.loads(capsys.readouterr().out)["mode"] == "live"


def test_eval_command_fails_a_live_run_where_no_backend_answered(home, monkeypatch, capsys):
    monkeypatch.delenv("CI", raising=False)
    write_corpus(home, [case("typo", True, noul(0.9))])
    monkeypatch.setattr(evaluation, "live_backends", lambda: [StubBackend("haiku")])
    assert cli.classifier_main(["eval", "sample", "--live", "1"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert (report["wrong"], report["samples"], report["failures"]) == (0, 0, 1)


def test_eval_command_refuses_a_negative_live_count(home, capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.classifier_main(["eval", "sample", "--live", "-1"])
    assert stopped.value.code == 2
    assert capsys.readouterr().err.endswith("error: --live needs a count of zero or more\n")


def test_eval_command_reports_a_metrics_failure_and_still_scores(home, monkeypatch, capsys):
    write_corpus(home, [case("typo", True, noul(0.9))])
    monkeypatch.setattr(evaluation, "record", lambda result, now_ms: ["metrics outbox failed: disk full"])
    assert cli.classifier_main(["eval", "sample"]) == 0
    captured = capsys.readouterr()
    assert captured.err == "classifier eval: metrics outbox failed: disk full\n"
    assert json.loads(captured.out)["wrong"] == 0


def test_eval_writes_classifier_rows_to_the_metrics(home, tmp_path, monkeypatch):
    spool = tmp_path / "metrics.sqlite"
    sent = []
    monkeypatch.setattr(metrics_outbox, "spool_path", lambda: spool)
    monkeypatch.setattr(metrics_outbox, "post", lambda sink, query, body: sent.append(query) or True)
    write_corpus(home, [case("typo", True, noul(0.9, latency=40), noul(0.2, source="haiku", latency=700))])
    result = evaluation.evaluate("sample")
    assert evaluation.record(result, 1_800_000_000_000, ON) == []
    assert sent[0].startswith("CREATE TABLE IF NOT EXISTS swarm.classifier_evals (")
    with closing(sqlite3.connect(spool)) as db:
        rows = [json.loads(row) for (row,) in db.execute("SELECT row FROM spool ORDER BY event_id")]
    digest = result.definition.digest
    assert rows == [
        {
            "event_id": f"classifier-eval:sample:replay:1800000000000:{source}:typo:{index}",
            "ledger": "sw",
            "ts_ms": 1_800_000_000_000,
            "plan": "",
            "phase": "",
            "slice": "",
            "task": "",
            "classifier": "sample",
            "digest": digest,
            "mode": "replay",
            "backend": source,
            "corpus_case": "typo",
            "sample": index,
            "control": 0,
            "outcome": outcome,
            "latency_ms": latency,
        }
        for source, index, outcome, latency in (("haiku", 1, "miss", 700), ("liquid-d1", 0, "hit", 40))
    ]


def test_eval_metrics_are_off_without_settings(home, tmp_path, monkeypatch):
    spool = tmp_path / "metrics.sqlite"
    monkeypatch.setattr(metrics_outbox, "spool_path", lambda: spool)
    write_corpus(home, [case("typo", True, noul(0.9))])
    assert evaluation.record(evaluation.evaluate("sample"), 1_800_000_000_000, {}) == []
    assert not spool.exists()


@pytest.mark.parametrize("name", sorted(path.name.split(".")[0] for path in PACKAGE.glob("*.corpus.yaml")))
def test_every_packaged_corpus_replays_clean(name):
    report = evaluation.evaluate(name).report()
    assert report["wrong_cases"] == []
    assert report["held_controls"] == sorted(
        item["name"]
        for item in yaml.safe_load((PACKAGE / f"{name}.corpus.yaml").read_text())["cases"]
        if item["control"]
    )
