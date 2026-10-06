import math

import pytest

from hooks.classifier import Choice, Score, YesNo
from hooks.classifier.errors import BackendFailure
from hooks.classifier.fallback_schema import answer_schema, normalize_answers

QUESTIONS = {
    "yes": YesNo("Is it simple?", true="simple", false="complex"),
    "tier": Choice("Which tier?", {"small": "typo", "large": "design"}),
    "effort": Score("How much effort?", ["low", "medium", "high"]),
}
RAW = {
    "answers": {
        "yes": {"noul": 0.75},
        "tier": {"probabilities": {"small": 0.2, "large": 0.6}},
        "effort": {"probabilities": {"0": 0.1, "1": 0.2, "2": 0.1}},
    }
}


def test_schema_requires_all_questions_and_probabilities():
    schema = answer_schema(QUESTIONS)
    assert schema["type"] == "object"
    assert schema["required"] == ["answers"]
    assert schema["additionalProperties"] is False
    answers = schema["properties"]["answers"]
    assert answers["required"] == list(QUESTIONS)
    assert answers["additionalProperties"] is False
    yes = answers["properties"]["yes"]
    assert yes == {
        "type": "object",
        "properties": {"noul": {"type": "number", "minimum": 0, "maximum": 1}},
        "required": ["noul"],
        "additionalProperties": False,
    }
    for name, keys in (("tier", ["small", "large"]), ("effort", ["0", "1", "2"])):
        obj = answers["properties"][name]
        assert obj["required"] == ["probabilities"]
        assert obj["additionalProperties"] is False
        probabilities = obj["properties"]["probabilities"]
        assert probabilities["required"] == keys
        assert probabilities["additionalProperties"] is False
        assert probabilities["properties"] == dict.fromkeys(keys, {"type": "number", "minimum": 0, "maximum": 1})


def test_normalizes_and_derives_answers():
    answers = normalize_answers(RAW, QUESTIONS)
    assert answers["yes"].to_dict() == {"type": "noul", "noul": 0.75}
    tier = answers["tier"]
    assert tier.type == "choice"
    assert tier.choice == "large"
    assert tier.confidence == pytest.approx(0.75)
    assert tier.probabilities == pytest.approx({"small": 0.25, "large": 0.75})
    effort = answers["effort"]
    assert effort.type == "score"
    assert effort.score == pytest.approx(1.0)
    assert effort.confidence == pytest.approx(0.5)
    assert effort.probabilities == pytest.approx({"0": 0.25, "1": 0.5, "2": 0.25})
    assert effort.legend == {"0": "low", "1": "medium", "2": "high"}


@pytest.mark.parametrize("value", [None, [], {}, {"answers": []}, {"answers": {"yes": {"noul": 0.1}}}])
def test_rejects_wrong_shape(value):
    with pytest.raises(BackendFailure, match="^parse error"):
        normalize_answers(value, QUESTIONS)


@pytest.mark.parametrize("value", [-0.1, 1.1, 10**400, math.nan, math.inf, "0.2", True, None])
def test_rejects_invalid_yes_probability(value):
    with pytest.raises(BackendFailure, match="^parse error"):
        normalize_answers({"answers": {"yes": {"noul": value}}}, {"yes": QUESTIONS["yes"]})


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"small": 1},
        {"small": 0, "large": 0},
        {"small": -1, "large": 1},
        {"small": 0.2, "large": math.nan},
        {"small": 0.2, "large": math.inf},
        {"small": 0.2, "large": "0.8"},
        {"small": True, "large": 0.2},
        {"small": 0.2, "large": 0.8, "extra": 0.1},
    ],
)
def test_rejects_invalid_distribution(value):
    with pytest.raises(BackendFailure, match="^parse error"):
        normalize_answers({"answers": {"tier": {"probabilities": value}}}, {"tier": QUESTIONS["tier"]})


def test_uses_order_for_tied_choices_and_zero_probabilities():
    raw = {
        "answers": {
            "tier": {"probabilities": {"small": 0.5, "large": 0.5}},
            "effort": {"probabilities": {"0": 0, "1": 0, "2": 1}},
        }
    }
    answers = normalize_answers(raw, {k: QUESTIONS[k] for k in ("tier", "effort")})
    assert answers["tier"].choice == "small"
    assert answers["effort"].score == 2
    assert answers["effort"].confidence == 1
