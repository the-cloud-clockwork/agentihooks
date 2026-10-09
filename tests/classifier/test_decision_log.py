import hashlib
import json
from datetime import datetime
from pathlib import Path

from hooks import config
from hooks.classifier import decision_log
from hooks.classifier.result import Answer, DecisionResult

ANSWERED = DecisionResult({"a": Answer("noul", noul=0.9)}, source="liquid-d1", cost=0.5)


def _lines():
    return [json.loads(line) for line in decision_log.log_path().read_text().splitlines()]


def test_log_lives_under_agentihooks_home():
    assert decision_log.log_path() == config.AGENTIHOOKS_HOME / "classifier" / "decisions.jsonl"


def test_state_digest_is_the_first_16_hex_of_the_canonical_sha256():
    state = {"b": 1, "a": [Path("/x")]}
    canonical = json.dumps({"a": ["/x"], "b": 1})
    assert decision_log.state_digest(state) == hashlib.sha256(canonical.encode()).hexdigest()[:16]
    assert decision_log.state_digest({"a": 1, "b": 2}) == decision_log.state_digest({"b": 2, "a": 1})
    assert decision_log.state_digest("one") != decision_log.state_digest("two")


def test_append_creates_missing_parents_and_appends(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "AGENTIHOOKS_HOME", tmp_path / "deep" / "home")
    decision_log.append("p", "s", ANSWERED, 12)
    decision_log.append("p", "s", None, 7)
    assert len(_lines()) == 2


def test_answered_line_shape():
    decision_log.append("model-pick", {"task": "t"}, ANSWERED, 12)
    (line,) = _lines()
    ts = line.pop("ts")
    assert datetime.fromisoformat(ts).utcoffset().total_seconds() == 0
    assert "." not in ts
    assert line == {
        "purpose": "model-pick",
        "source": "liquid-d1",
        "calibrated": True,
        "latency_ms": 12,
        "cost": 0.5,
        "answers": {"a": {"type": "noul", "noul": 0.9}},
        "state_digest": decision_log.state_digest({"task": "t"}),
        "failures": [],
        "api_down_cached": False,
        "definition": None,
        "definition_digest": None,
        "expected": None,
    }


def test_unanswered_line_shape():
    decision_log.append("gate", "s", None, 7)
    (line,) = _lines()
    line.pop("ts")
    assert line == {
        "purpose": "gate",
        "source": None,
        "calibrated": None,
        "latency_ms": 7,
        "cost": None,
        "answers": {},
        "state_digest": decision_log.state_digest("s"),
        "failures": [],
        "api_down_cached": False,
        "definition": None,
        "definition_digest": None,
        "expected": None,
    }


def test_stats_of_a_single_call():
    decision_log.append("p", "s", ANSWERED, 42)
    assert decision_log.stats()["latency_ms"] == {"p50": 42, "p90": 42, "p99": 42}


def test_stats_rounds_rate_to_four_places_and_cost_to_nine():
    fallback = DecisionResult({"a": Answer("noul", noul=0.6)}, source="haiku", calibrated=False)
    cheap = DecisionResult({"a": Answer("noul", noul=0.9)}, source="jev-1.13", cost=1.234567891234e-05)
    for result in (cheap, cheap, fallback):
        decision_log.append("p", "s", result, 1)
    out = decision_log.stats()
    assert out["fallback_rate"] == 0.3333
    assert out["cost"] == 2.4691e-05


def test_stats_filters_by_purpose_and_reads_an_absent_log_as_empty():
    assert decision_log.read() == []
    decision_log.append("a", "s", ANSWERED, 1)
    decision_log.append("b", "s", ANSWERED, 1)
    assert [e["purpose"] for e in decision_log.read("b")] == ["b"]
    assert len(decision_log.read()) == 2
