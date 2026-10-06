from __future__ import annotations

import re
from dataclasses import dataclass, field

from hooks.classifier.errors import ClassifierInputError

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_QUESTIONS = 128
MAX_OPTIONS = 255
MAX_LEVELS = 10


@dataclass(frozen=True)
class YesNo:
    instructions: str
    true: str
    false: str
    type: str = field(default="noul", init=False)

    def criteria(self) -> dict:
        return {"true": self.true, "false": self.false}


@dataclass(frozen=True)
class Choice:
    instructions: str
    options: dict
    type: str = field(default="choice", init=False)

    def criteria(self) -> dict:
        return dict(self.options)


@dataclass(frozen=True)
class Score:
    instructions: str
    levels: list
    type: str = field(default="score", init=False)

    def criteria(self) -> list:
        return list(self.levels)


Question = YesNo | Choice | Score


def _check_one(name: str, question: object) -> None:
    if not NAME_PATTERN.match(name):
        raise ClassifierInputError(f"question name {name!r} must match {NAME_PATTERN.pattern}")
    if not isinstance(question, (YesNo, Choice, Score)):
        raise ClassifierInputError(f"question {name!r} must be a YesNo, Choice or Score")
    if isinstance(question, Choice) and not 1 <= len(question.options) <= MAX_OPTIONS:
        raise ClassifierInputError(f"choice {name!r} needs 1 to {MAX_OPTIONS} options")
    if isinstance(question, Score) and not 1 <= len(question.levels) <= MAX_LEVELS:
        raise ClassifierInputError(f"score {name!r} needs 1 to {MAX_LEVELS} levels")


def validate(questions: dict) -> None:
    if not 1 <= len(questions) <= MAX_QUESTIONS:
        raise ClassifierInputError(f"ask 1 to {MAX_QUESTIONS} questions, got {len(questions)}")
    for name, question in questions.items():
        _check_one(name, question)


def wire_questions(questions: dict) -> dict:
    return {
        name: {"type": q.type, "instructions": q.instructions, "criteria": q.criteria()}
        for name, q in questions.items()
    }


def _from_wire(name: str, raw: object) -> Question:
    if not isinstance(raw, dict) or not {"type", "instructions", "criteria"} <= raw.keys():
        raise ClassifierInputError(f"question {name!r} needs type, instructions and criteria")
    kind, text, criteria = raw["type"], raw["instructions"], raw["criteria"]
    if kind == "noul":
        return YesNo(text, true=criteria["true"], false=criteria["false"])
    if kind == "choice":
        return Choice(text, dict(criteria))
    if kind == "score":
        return Score(text, list(criteria))
    raise ClassifierInputError(f"question {name!r} has unknown type {kind!r}")


def questions_from_wire(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise ClassifierInputError("questions must be a JSON object of named questions")
    return {name: _from_wire(name, question) for name, question in raw.items()}
