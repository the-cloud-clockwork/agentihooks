"""Exit expressions: deterministic stage and outcome forms, free text routed to a classifier."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

Classify = Callable[[str, str], bool]

CLASSIFIER_PROFILE = "You judge whether the given outputs satisfy the condition, and answer only through the schema."
CLASSIFIER_TIER = "low"
CLASSIFIER_EFFORT = "low"
CLASSIFIER_SCHEMA = {
    "type": "object",
    "properties": {"satisfied": {"type": "boolean"}},
    "required": ["satisfied"],
    "additionalProperties": False,
}


def classify_with_headless(expression: str, outputs_text: str) -> bool:
    raise NotImplementedError


def check(expression: str, outputs: Any, classify: Classify | None = None) -> bool:
    raise NotImplementedError
