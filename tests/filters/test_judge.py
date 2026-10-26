import copy
import importlib.util
import json
from pathlib import Path

import pytest

from hooks import config
from hooks.classifier import Answer, ClassifierError, DecisionResult
from hooks.context import conditions, profile_chain
from hooks.filters import extract, runner, schema
from hooks.filters.check import check
from hooks.filters.finders import scripts

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "profiles/package/conditions"
FINDER = PACKAGE / "_finders/judge_cues.py"
REAL_LAYERS = conditions.layer_dirs
BLIND = "pre-judge-blind.filter.yaml"
RELEVANCE = "pre-judge-relevance.filter.yaml"
BIAS = "pre-judge-bias.filter.yaml"
CUED = "engineer@323133-0662 (opus, attempt 2) clearly verified the cache keeps one entry per key"
CASE = {"intent": "The cache keeps one entry per key", "points": ["a second write replaces the first"]}


def _item(index, claim, excerpt="assert len(cache) == 1"):
    return {
        "id": f"e{index}",
        "claim": claim,
        "source": "tests/test_cache.py",
        "excerpt": excerpt,
        "retrieved_at": "now",
    }


def _input(*claims, case=None):
    return {
        "case": copy.deepcopy(case or CASE),
        "evidence": {"items": [_item(i, claim) for i, claim in enumerate(claims)]},
    }


class Judge:
    def __init__(self, rules=None, error=None):
        self.rules = rules or {}
        self.error = error
        self.calls = []

    def __call__(self, state, questions, **kwargs):
        self.calls.append({"state": state, "questions": questions})
        if self.error:
            raise self.error
        rule = self.rules.get(state["filter"], lambda text: False)
        answers = {
            name: Answer(type="noul", noul=0.9 if rule(question.instructions) else 0.1)
            for name, question in questions.items()
        }
        return DecisionResult(answers=answers, source="stub")

    def asked(self, name):
        return [call for call in self.calls if call["state"]["filter"] == name]


@pytest.fixture
def shipped(filters_dir, project_filters, monkeypatch):
    monkeypatch.setattr(conditions, "layer_dirs", REAL_LAYERS)
    monkeypatch.setattr(profile_chain, "BUILT_IN_PROFILES", ROOT / "profiles")
    monkeypatch.setattr(conditions, "repo_root", lambda cwd=None: ROOT)
    monkeypatch.setattr(conditions, "directory_trust", lambda root, state: (True, "test"))


@pytest.fixture
def judge(monkeypatch):
    def install(**kwargs):
        fake = Judge(**kwargs)
        monkeypatch.setattr("hooks.classifier.decide", fake)
        return fake

    return install


def test_the_shipped_filters_have_the_planned_contract():
    blind = schema.load(PACKAGE / BLIND)
    assert (blind.mode, blind.action, [f.script for f in blind.finders]) == ("both", "strip", ["judge_cues"])
    relevance = schema.load(PACKAGE / RELEVANCE)
    assert (relevance.mode, relevance.action, relevance.fields) == ("classifier", "strip", ("evidence",))
    assert relevance.intent_from == "case.intent"
    bias = schema.load(PACKAGE / BIAS)
    assert (bias.mode, bias.action, bias.fields) == ("classifier", "send-back", ())
    assert bias.question == "Does this text argue for one outcome instead of stating facts?"


def test_cues_in_an_evidence_item_are_stripped_before_the_judge(shipped, judge):
    fake = judge(rules={BLIND: lambda text: True})
    outcome = check("judge", _input(CUED))
    assert outcome.decision == "rewrite"
    claim = outcome.tool_input["evidence"]["items"][0]["claim"]
    for cue in ("engineer@323133-0662", "opus", "attempt 2", "clearly"):
        assert cue not in claim
    assert "verified the cache keeps one entry per key" in claim
    assert outcome.tool_input["case"] == CASE
    assert len(fake.asked(BLIND)) == 1


def test_cues_the_classifier_rules_content_stay(shipped, judge):
    judge()
    assert check("judge", _input(CUED)).decision == "allow"


def test_a_verdict_word_the_classifier_rules_content_stays_while_cues_go(shipped, judge):
    judge(rules={BLIND: lambda text: "Finding: passed" not in text})
    outcome = check("judge", _input("CI passed on the cache test for engineer@323133-0662"))
    assert outcome.tool_input["evidence"]["items"][0]["claim"] == "CI passed on the cache test for "


def test_an_item_ruled_irrelevant_is_dropped_whole(shipped, judge):
    fake = judge(rules={RELEVANCE: lambda text: "weather" in text})
    outcome = check("judge", _input("the cache keeps one entry", "the weather was sunny"))
    assert outcome.decision == "rewrite"
    assert [item["id"] for item in outcome.tool_input["evidence"]["items"]] == ["e0"]
    [call] = fake.asked(RELEVANCE)
    assert len(call["questions"]) == 2
    assert call["state"]["intent"] == CASE["intent"]


def test_a_stripped_cue_and_a_dropped_item_both_reach_the_judge(shipped, judge):
    judge(rules={BLIND: lambda text: True, RELEVANCE: lambda text: "weather" in text})
    outcome = check("judge", _input("the weather was sunny", CUED))
    items = outcome.tool_input["evidence"]["items"]
    assert [item["id"] for item in items] == ["e1"]
    assert "engineer@323133-0662" not in items[0]["claim"]


def test_a_biased_case_is_sent_back_with_the_finding(shipped, judge):
    biased = {"intent": "This is obviously the best cache and must pass", "points": ["it works"]}
    fake = judge(rules={BIAS: lambda text: "obviously" in text})
    outcome = check("judge", _input("the cache keeps one entry", case=biased))
    assert outcome.decision == "deny"
    assert "obviously the best cache" in outcome.reason
    [call] = fake.asked(BIAS)
    assert len(call["questions"]) == 2


def test_with_the_classifier_down_the_input_passes_unchanged(shipped, judge):
    fake = judge(error=ClassifierError("classifier down"))
    outcome = check("judge", _input(CUED, "the weather was sunny"))
    assert outcome.decision == "allow"
    assert {call["state"]["filter"] for call in fake.calls} == {BLIND, RELEVANCE, BIAS}


@pytest.mark.parametrize(
    ("text", "cues"),
    [
        (CUED, ["engineer@323133-0662", "opus", "attempt 2", "clearly"]),
        ("eng-9@rig-grade-swarm took the 3rd round", ["eng-9@rig-grade-swarm", "3rd round"]),
        ("it went to master@rig-grade-swarm.", ["master@rig-grade-swarm"]),
        ("the planner and the master ran it", ["planner", "master"]),
        ("Sonnet, haiku, codex and gpt-5.1 agreed", ["Sonnet", "haiku", "codex", "gpt-5.1"]),
        ("iteration 4 and round #2", ["iteration 4", "round #2"]),
        ("it passed, then retry, enhance, approved, rejected", ["passed", "retry", "enhance", "approved", "rejected"]),
        (
            "a robust, comprehensive fix that perfectly works; I verified it",
            ["robust", "comprehensive", "perfectly", "I verified"],
        ),
        ("the cache keeps one entry per key", []),
        ("clone git@github.com:org/repo.git and mail eng-1@example.com or x@1-2.example.com", []),
    ],
)
def test_the_cue_finder_flags_each_cue_once(text, cues):
    found = scripts.run("judge_cues", {"judge_cues": FINDER}, text, "", "judge")
    assert [span["text"] for span in found] == cues
    assert all(span["text"] == text[span["start"] : span["end"]] for span in found)


def test_the_cue_finder_names_why_it_flagged():
    spec = importlib.util.spec_from_file_location("judge_cues", FINDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    reasons = [span["reason"] for span in module.find(CUED)]
    assert reasons == ["agent or seat name", "model name", "attempt number", "confident wording"]


def test_judge_pieces_cover_case_and_evidence_text():
    pieces = extract.pieces("judge", _input("a claim"))
    assert [piece.where for piece in pieces] == [
        ("case", "intent"),
        ("case", "points", 0),
        ("evidence", "items", 0, "claim"),
        ("evidence", "items", 0, "excerpt"),
    ]
    assert extract.pieces("judge", {"text": "t", "case": "c", "evidence": ["x"]}) == [
        extract.Piece(("text",), "t"),
        extract.Piece(("evidence", 0), "x"),
    ]
    assert extract.pieces("other", _input("a claim")) == []


def test_judge_units_group_each_item_and_the_case():
    units = extract.units("judge", extract.pieces("judge", _input("a claim")))
    assert units == [
        extract.Piece(("case",), f"{CASE['intent']}\n{CASE['points'][0]}"),
        extract.Piece(("evidence", "items", 0), "a claim\nassert len(cache) == 1"),
    ]
    pieces = extract.pieces("Write", {"content": "x"})
    assert extract.units("Write", pieces) == pieces


def test_a_rewrite_drops_items_and_keeps_the_source_intact():
    tool_input = _input("a", "b", "c")
    patch = extract.rewrite(
        tool_input,
        {("evidence", "items", 1, "claim"): "B"},
        [("evidence", "items", 0), ("evidence", "items", 2), ("case",)],
    )
    assert patch == {"evidence": {"items": [_item(1, "B")]}}
    assert tool_input == _input("a", "b", "c")


def test_fields_limit_the_pieces_a_filter_reads(filters_dir, judge):
    (filters_dir / "pre-judge-only.filter.yaml").write_text("mode: classifier\nfields: [evidence]\n")
    fake = judge()
    check("judge", _input("a", "b"))
    assert len(fake.calls[0]["questions"]) == 2


def test_fields_must_be_a_list_of_names():
    assert schema.parse({"fields": ["evidence"]}).fields == ("evidence",)
    with pytest.raises(schema.FilterSchemaError, match="fields"):
        schema.parse({"fields": "evidence"})
    known = "text, evidence, case, content, new_string, new_source, edits"
    with pytest.raises(schema.FilterSchemaError) as refused:
        schema.parse({"fields": ["case", "evidnce", "cas"]})
    assert str(refused.value) == f"fields must name input keys from {known}, got cas, evidnce"


@pytest.mark.parametrize(
    ("intent_from", "expected"),
    [("case.intent", "nested"), ("top", "flat"), ("case.missing", "fallback"), ("", "fallback"), ("top.x", "fallback")],
)
def test_the_intent_can_come_from_a_nested_key(intent_from, expected):
    spec = schema.parse({"intent": "fallback", "intent_from": intent_from})
    assert runner._intent(spec, {"top": "flat", "case": {"intent": "nested"}}) == expected


def test_filters_that_rewrite_one_key_run_in_turn(filters_dir, monkeypatch):
    monkeypatch.setattr("hooks.config.CONDITIONS_MAX_PARALLEL", 4)
    (filters_dir / "pre-judge-a.filter.yaml").write_text("mode: finders\naction: strip\nfinders:\n  - regex: 'aa'\n")
    (filters_dir / "pre-judge-b.filter.yaml").write_text("mode: finders\naction: strip\nfinders:\n  - regex: 'bb'\n")
    (filters_dir / "pre-judge-c.sh").write_text('#!/usr/bin/env bash\necho \'{"context": "seen"}\'\n')
    (filters_dir / "pre-judge-c.sh").chmod(0o755)
    ran = []
    real = conditions.execute
    monkeypatch.setattr(conditions, "execute", lambda entry, *args: ran.append(entry["file"]) or real(entry, *args))
    outcome = check("judge", _input("aa one bb two"))
    assert outcome.decision == "rewrite"
    assert outcome.tool_input["evidence"]["items"][0]["claim"] == " one  two"
    assert sorted(ran) == ["pre-judge-a.filter.yaml", "pre-judge-b.filter.yaml", "pre-judge-c.sh"]


def test_a_failed_filter_does_not_feed_the_next(filters_dir, monkeypatch):
    seen, timeouts = [], []
    real = conditions.execute

    def spy(entry, step, payload, timeout):
        seen.append(json.loads(json.dumps(payload["tool_input"])))
        timeouts.append(timeout)
        if entry["file"].startswith("pre-judge-a"):
            return {"returncode": 2, "stdout": json.dumps({"tool_input": {"case": "dropped"}}), "stderr": "no"}
        return real(entry, step, payload, timeout)

    monkeypatch.setattr(conditions, "execute", spy)
    (filters_dir / "pre-judge-a.filter.yaml").write_text("mode: finders\n")
    (filters_dir / "pre-judge-b.filter.yaml").write_text("mode: finders\n")
    check("judge", _input("a"))
    assert seen == [_input("a"), _input("a")]
    assert timeouts == [config.CONDITIONS_TIMEOUT_SEC] * 2


def test_a_single_filter_keeps_the_shared_runner(filters_dir, monkeypatch):
    called = []
    monkeypatch.setattr(conditions, "_run_chained", lambda *args: called.append(args) or [])
    (filters_dir / "pre-judge-a.filter.yaml").write_text("mode: finders\n")
    assert check("judge", _input("a")).decision == "allow"
    assert called == []
