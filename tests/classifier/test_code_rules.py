import pytest
import yaml

from hooks.classifier import code_rules, corpus, definitions, evaluation, runner
from hooks.classifier.questions import YesNo
from hooks.classifier.result import Answer, DecisionResult

from .test_definitions import definition_home as definition_home
from .test_definitions import sample, write_definition

TARGET = "tests.classifier.test_code_rules:RULE"


def _questions(definition, state, params):
    asked = runner.questions_for(definition, params)
    if state.get("wide") or params.get("wide"):
        asked["second"] = YesNo("Second?", "yes", "no")
    return asked


def _verdicts(definition, state, params, answers):
    go = answers["accept"].noul >= definition.thresholds["yes"]
    return {"verdict": "go" if go else "stop", "params": params, "state": state}


RULE = code_rules.CodeRule(_questions, _verdicts, {"verdict": ("go", "stop")}, {"verdict": "stop"})


def noul(value, *keys, source="liquid-d1", latency=40):
    keys = keys or ("accept",)
    return {"source": source, "latency_ms": latency, "answers": {k: {"type": "noul", "noul": value} for k in keys}}


def case(name, verdict, *samples, control=False, state=None, **extra):
    return {
        "name": name,
        "state": {"task": name} if state is None else state,
        "expected": {"verdict": verdict},
        "control": control,
        "samples": list(samples),
        **extra,
    }


def write_corpus(folder, cases, **extra):
    (folder / "sample.corpus.yaml").write_text(yaml.safe_dump({"version": 1, "cases": cases, **extra}))


@pytest.fixture
def home(definition_home, monkeypatch):
    raw = sample()
    raw["rule"] = {"type": "code"}
    write_definition(definition_home, raw)
    monkeypatch.setitem(code_rules.RULES, "sample", TARGET)
    return definition_home


def load():
    return corpus.load(definitions.load("sample"), corpus.path_for("sample"))


def test_rule_for_resolves_a_registered_code_rule(home):
    assert code_rules.rule_for(definitions.load("sample")) is RULE


def test_rule_for_ignores_an_unregistered_code_rule(home, monkeypatch):
    monkeypatch.delitem(code_rules.RULES, "sample")
    assert code_rules.rule_for(definitions.load("sample")) is None


def test_rule_for_ignores_a_registered_name_without_a_code_rule(definition_home, monkeypatch):
    write_definition(definition_home, sample())
    monkeypatch.setitem(code_rules.RULES, "sample", TARGET)
    assert code_rules.rule_for(definitions.load("sample")) is None


def test_the_packaged_intent_definitions_resolve_the_intent_rule():
    from scripts.gates import intent

    for name in ("intent-check", "intent-check-tests-first"):
        assert code_rules.rule_for(definitions.load(name)) is intent.RULE


def test_runner_asks_the_rule_questions_and_returns_its_verdicts(home):
    calls = []

    def decider(state, questions, **kwargs):
        calls.append((state, list(questions), kwargs))
        return DecisionResult({"accept": Answer("noul", noul=0.7)}, "recorded")

    output = runner.run("sample", {"wide": True}, decider=decider)
    assert calls == [({"wide": True}, ["accept", "second"], {"purpose": "sample", "fallbacks": []})]
    assert output.verdicts == {"verdict": "go", "params": {}, "state": {"wide": True}}
    output = runner.run("sample", {}, {"p": 1}, decider=decider)
    assert output.verdicts == {"verdict": "go", "params": {"p": 1}, "state": {}}


def test_runner_keeps_empty_verdicts_for_an_unregistered_code_rule(home, monkeypatch):
    monkeypatch.delitem(code_rules.RULES, "sample")
    result = DecisionResult({"accept": Answer("noul", noul=0.7)}, "recorded")
    assert runner.run("sample", {}, decider=lambda *args, **kwargs: result).verdicts == {}


def test_replay_runs_the_rule_over_each_recorded_sample(home):
    write_corpus(
        home,
        [
            case("keep", "go", noul(0.9), noul(0.2, latency=70)),
            case("drop", "stop", noul(0.1), noul(0.3), control=True),
            case("wide", "go", noul(0.8, "accept", "second"), state={"wide": True}, params={"p": 2}),
        ],
    )
    outcomes = evaluation.replay(definitions.load("sample"), load())
    assert [(item.case.name, item.sample, item.latency_ms, item.outcome) for item in outcomes] == [
        ("keep", 0, 40, "hit"),
        ("keep", 1, 70, "miss"),
        ("drop", 0, 40, "hit"),
        ("drop", 1, 40, "hit"),
        ("wide", 0, 40, "hit"),
    ]
    assert outcomes[-1].verdicts == {"verdict": "go", "params": {"p": 2}, "state": {"wide": True}}
    report = evaluation.evaluate("sample").report()
    assert (report["wrong"], report["wrong_cases"], report["held_controls"]) == (1, ["keep"], ["drop"])


def test_live_asks_the_rule_questions(home, monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    write_corpus(home, [case("wide", "go", noul(0.8, "accept", "second"), state={"wide": True})])
    seen = []

    class Backend:
        name = "stub"

        def decide(self, request):
            seen.append(list(request.questions))
            return DecisionResult({"accept": Answer("noul", noul=0.9)}, "stub")

    report = evaluation.evaluate("sample", 1, [Backend()]).report()
    assert seen == [["accept", "second"]]
    assert (report["samples"], report["wrong"]) == (1, 0)


def test_baseline_outcomes_score_the_recorded_verdicts(home):
    write_corpus(
        home,
        [
            case("keep", "go", noul(0.9), baseline=[{"verdict": "go"}, {"verdict": "stop"}]),
            case("drop", "stop", noul(0.1), control=True, baseline=[{"verdict": "stop"}]),
        ],
    )
    cases = load()
    outcomes = evaluation.baseline(cases)
    assert [(item.case.name, item.source, item.sample, item.latency_ms, item.outcome) for item in outcomes] == [
        ("keep", "baseline", 0, 0, "hit"),
        ("keep", "baseline", 1, 0, "miss"),
        ("drop", "baseline", 0, 0, "hit"),
    ]
    scored = evaluation.score(cases, list(outcomes))
    assert (scored["samples"], scored["wrong"], scored["wrong_cases"], scored["held_controls"]) == (
        3,
        1,
        ["keep"],
        ["drop"],
    )


def test_a_case_keeps_its_notes_and_the_corpus_may_carry_notes(home):
    write_corpus(home, [case("keep", "go", noul(0.9), notes={"labels": {"spec": "ok"}})], notes={"about": "x"})
    (loaded,) = load()
    assert (loaded.notes, loaded.baseline) == ({"labels": {"spec": "ok"}}, ())


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (case("c", "go", noul(0.9), baseline={"verdict": "go"}), "case c baseline must be a list of recorded verdicts"),
        (
            case("c", "go", noul(0.9), baseline=[{"verdict": "maybe"}]),
            "case c expected verdict is not a verdict its rule can give",
        ),
        (case("c", "go", noul(0.9), baseline=[{}]), "case c expected must name exactly: verdict"),
        ({**case("c", "go", noul(0.9)), "expected": {"accept": True}}, "case c expected must name exactly: verdict"),
        (case("c", "maybe", noul(0.9)), "case c expected verdict is not a verdict its rule can give"),
        (case("c", "go", noul(0.9), control=True), "control case c must expect only rejections"),
        (case("c", "go", noul(0.9), state={"wide": True}), "case c sample 0 answers must name exactly: accept, second"),
    ],
)
def test_load_checks_a_code_rule_case_against_its_rule(home, raw, message):
    write_corpus(home, [raw])
    with pytest.raises(corpus.CorpusError) as error:
        load()
    assert str(error.value) == message


def test_a_rule_definition_checks_its_baseline_against_its_questions(definition_home):
    write_definition(definition_home, sample())
    good = {**case("c", True, noul(0.9), baseline=[{"accept": False}]), "expected": {"accept": True}}
    write_corpus(definition_home, [good])
    assert [item.baseline for item in load()] == [({"accept": False},)]
    write_corpus(definition_home, [{**good, "baseline": [{"accept": "x"}]}])
    with pytest.raises(corpus.CorpusError) as error:
        load()
    assert str(error.value) == "case c expected accept is not a verdict its rule can give"


def test_an_unregistered_code_rule_is_still_refused(home, monkeypatch):
    monkeypatch.delitem(code_rules.RULES, "sample")
    write_corpus(home, [case("c", "go", noul(0.9))])
    with pytest.raises(corpus.CorpusError) as error:
        load()
    assert str(error.value) == "classifier sample keeps a code rule; replay needs a yes, choice or score rule"
