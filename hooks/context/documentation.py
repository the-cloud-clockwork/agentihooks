from collections.abc import Callable
from typing import Any, cast

from hooks.classifier import code_rules, runner
from hooks.classifier.definitions import Definition
from hooks.classifier.result import Answer


def _verdicts(
    definition: Definition, state: object, params: dict[str, Any], answers: dict[str, Answer]
) -> dict[str, Any]:
    needed, covered, size = (answers[key] for key in ("needed", "covered", "size"))
    high, low = definition.thresholds["yes"], definition.thresholds["no"]
    decision = None
    if cast(float, needed.noul) <= low or cast(float, covered.noul) >= high:
        decision = False
    elif cast(float, needed.noul) >= high and cast(float, covered.noul) <= low and (size.confidence or 0) >= high:
        decision = True
    return {"needs_documentation": decision, "size": size.choice}


RULE = code_rules.CodeRule(
    code_rules.asked,
    _verdicts,
    {"needs_documentation": (True, False, None), "size": ("none", "small", "medium", "large", None)},
    {"needs_documentation": False, "size": "none"},
)


def classify(evidence: dict[str, Any], *, decider: Callable[..., Any] | None = None) -> dict[str, Any]:
    output = runner.run("documentation-needed", evidence, decider=decider)
    return {
        **output.verdicts,
        "source": output.raw.source,
        "answers": {key: value.to_dict() for key, value in output.raw.answers.items()},
    }
