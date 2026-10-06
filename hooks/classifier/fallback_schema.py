import math

from hooks.classifier.errors import BackendFailure
from hooks.classifier.questions import Choice, Score, YesNo
from hooks.classifier.result import Answer


def _object(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _keys(question: Choice | Score) -> list[str]:
    return list(question.options) if isinstance(question, Choice) else [str(i) for i in range(len(question.levels))]


def answer_schema(questions: dict) -> dict:
    probability = {"type": "number", "minimum": 0, "maximum": 1}
    answers = {}
    for name, question in questions.items():
        if isinstance(question, YesNo):
            answers[name] = _object({"noul": probability})
        else:
            answers[name] = _object({"probabilities": _object(dict.fromkeys(_keys(question), probability))})
    return _object({"answers": _object(answers)})


def _probability(value: object) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise BackendFailure("parse error: invalid probability")
    return float(value)


def _answer(raw: dict, question: YesNo | Choice | Score) -> Answer:
    if isinstance(question, YesNo):
        return Answer(type="noul", noul=_probability(raw["noul"]))
    keys = _keys(question)
    values = raw["probabilities"]
    if set(values) != set(keys):
        raise BackendFailure("parse error: incomplete distribution")
    probabilities = {key: _probability(values[key]) for key in keys}
    total = sum(probabilities.values())
    if total == 0:
        raise BackendFailure("parse error: zero distribution")
    probabilities = {key: value / total for key, value in probabilities.items()}
    confidence = max(probabilities.values())
    if isinstance(question, Choice):
        return Answer(
            type="choice",
            choice=max(probabilities, key=probabilities.get),
            confidence=confidence,
            probabilities=probabilities,
        )
    return Answer(
        type="score",
        score=sum(int(key) * value for key, value in probabilities.items()),
        confidence=confidence,
        probabilities=probabilities,
        legend=dict(zip(keys, question.levels)),
    )


def normalize_answers(payload: object, questions: dict) -> dict:
    try:
        raw = payload["answers"]
        if set(raw) != set(questions):
            raise BackendFailure("parse error: incomplete answers")
        return {name: _answer(raw[name], question) for name, question in questions.items()}
    except (KeyError, TypeError, ValueError, AttributeError):
        raise BackendFailure("parse error: invalid answers") from None
