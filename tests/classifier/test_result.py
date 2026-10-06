import pytest

from hooks.classifier import YesNo
from hooks.classifier.errors import BackendFailure
from hooks.classifier.result import Answer, DecisionResult, parse_answers

QUESTIONS = {"a": YesNo("q", true="t", false="f"), "b": YesNo("q", true="t", false="f")}


def test_every_missing_answer_is_named():
    with pytest.raises(BackendFailure) as err:
        parse_answers({"answers": {}}, QUESTIONS, "jev-1.13")
    assert str(err.value) == "jev-1.13: no answer for a, b"


def test_answers_without_a_payload_key_are_missing():
    with pytest.raises(BackendFailure):
        parse_answers({}, QUESTIONS, "jev-1.13")


def test_extra_answers_are_ignored():
    answers = parse_answers(
        {"answers": {"a": {"type": "noul", "noul": 0.1}, "b": {"type": "noul", "noul": 0.2}, "c": {"type": "noul"}}},
        QUESTIONS,
        "jev-1.13",
    )
    assert answers == {"a": Answer("noul", noul=0.1), "b": Answer("noul", noul=0.2)}


def test_answer_to_dict_drops_empty_fields():
    assert Answer("noul", noul=0.0).to_dict() == {"type": "noul", "noul": 0.0}


def test_result_to_dict():
    result = DecisionResult({"a": Answer("noul", noul=0.5)}, source="haiku", calibrated=False, latency_ms=9)
    assert result.to_dict() == {
        "source": "haiku",
        "calibrated": False,
        "latency_ms": 9,
        "cost": None,
        "answers": {"a": {"type": "noul", "noul": 0.5}},
    }
