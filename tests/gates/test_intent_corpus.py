import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hooks.classifier.decision_log import state_digest
from scripts.gates import intent

CORPUS = Path(__file__).parents[1] / "fixtures" / "intent_calibration.json"


@pytest.fixture
def corpus():
    return json.loads(CORPUS.read_text())


def test_recorded_intent_decisions_match_independent_labels(corpus):
    outcomes = []
    for case in corpus["cases"]:
        record = case["baseline"]
        assert state_digest(case["state"]) == record["input_digest"]
        assert case["expected"] in ("pass", "fail")
        assert set(case["labels"]) == {"standards", "spec"}
        assert all(case["labels"].values())

        def recorded(state, questions, *, purpose):
            assert state == case["state"]
            assert purpose == intent.PURPOSE
            assert list(questions) == list(record["answers"])
            return SimpleNamespace(
                answers={name: SimpleNamespace(noul=value["noul"]) for name, value in record["answers"].items()}
            )

        verdict, _ = intent.judge(case["state"], decide=recorded)
        assert verdict == case["expected"], case["id"]
        outcomes.append(verdict)
    assert outcomes.count("pass") == 4
    assert outcomes.count("fail") == 5


def test_corpus_does_not_certify_historical_inputs_or_gate_promotion(corpus):
    assert corpus["historical_inputs_verified"] is False
    assert corpus["promotion"].startswith("observe;")
    assert {case["baseline"]["source"] for case in corpus["cases"]} == {"luna"}
    assert all(case["baseline"]["calibrated"] is False for case in corpus["cases"])
