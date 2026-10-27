"""Exit expressions: deterministic stage and outcome forms, free text routed to a classifier."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from scripts.headless import runner
from scripts.workflows.schema import OUTCOMES, STAGE_PASS, WorkflowError

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
    reply = runner.run(
        CLASSIFIER_PROFILE,
        f"condition: {expression}\noutputs: {outputs_text}",
        CLASSIFIER_SCHEMA,
        "sealed",
        CLASSIFIER_TIER,
        CLASSIFIER_EFFORT,
    )
    return reply["satisfied"]


def verdict_of(outputs: Any) -> Any:
    return outputs.get("verdict") if isinstance(outputs, dict) else None


def check(expression: str, outputs: Any, classify: Classify | None = None) -> bool:
    if not expression.strip():
        raise WorkflowError(["an exit expression must not be empty"])
    match = STAGE_PASS.match(expression)
    if match:
        stage = outputs.get(match.group(1)) if isinstance(outputs, dict) else None
        return verdict_of(stage) == "pass"
    if expression in OUTCOMES:
        return verdict_of(outputs) == expression
    judge = classify or classify_with_headless
    return judge(expression, json.dumps(outputs, sort_keys=True, default=str))
