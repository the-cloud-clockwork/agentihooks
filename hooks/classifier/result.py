from __future__ import annotations

import json
from dataclasses import dataclass, field

from hooks.classifier.errors import BackendFailure
from hooks.classifier.questions import wire_questions


@dataclass(frozen=True)
class Answer:
    type: str
    choice: str | None = None
    score: float | None = None
    noul: float | None = None
    confidence: float | None = None
    probabilities: dict = field(default_factory=dict)
    legend: dict = field(default_factory=dict)

    @classmethod
    def from_wire(cls, raw: dict) -> Answer:
        return cls(
            type=raw["type"],
            choice=raw.get("choice"),
            score=raw.get("score"),
            noul=raw.get("noul"),
            confidence=raw.get("confidence"),
            probabilities=dict(raw.get("probabilities") or {}),
            legend=dict(raw.get("legend") or {}),
        )

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None and v != {}}


@dataclass(frozen=True)
class DecisionRequest:
    state: object
    questions: dict

    def wire(self) -> dict:
        return {"state": self.state, "questions": wire_questions(self.questions)}

    def estimated_tokens(self) -> int:
        return len(json.dumps(self.wire())) // 4


@dataclass(frozen=True)
class DecisionResult:
    answers: dict
    source: str
    calibrated: bool = True
    latency_ms: int = 0
    cost: float | None = None

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "calibrated": self.calibrated,
            "latency_ms": self.latency_ms,
            "cost": self.cost,
            "answers": {name: answer.to_dict() for name, answer in self.answers.items()},
        }


def parse_answers(payload: dict, questions: dict, source: str) -> dict:
    raw = payload.get("answers") or {}
    missing = [name for name in questions if name not in raw]
    if missing:
        raise BackendFailure(f"{source}: parse error, no answer for {', '.join(missing)}")
    return {name: Answer.from_wire(raw[name]) for name in questions}
