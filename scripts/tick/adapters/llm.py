"""Llm port adapter: classifier decisions over the decisions API, with the call timeout taken from the deadline."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, cast

from hooks.classifier.api import DecisionsApiBackend
from hooks.classifier.result import DecisionRequest
from scripts.tick.adapters.inbox import check
from scripts.tick.deadline import Deadline

__all__ = ["ApiLlm"]


class Decider(Protocol):
    def decide(self, request: DecisionRequest) -> object: ...


@dataclass(frozen=True)
class ApiLlm:
    model: str
    url: str
    backend: Callable[[str, str, float], Decider] = DecisionsApiBackend

    def classify(self, request: Mapping[str, object], *, deadline: Deadline) -> object:
        check(deadline)
        decision = DecisionRequest(state=request["state"], questions=cast(dict[str, object], request["questions"]))
        return self.backend(self.model, self.url, deadline.remaining).decide(decision)
