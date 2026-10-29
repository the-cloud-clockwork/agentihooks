from collections.abc import Callable

from hooks.classifier import code_rules, runner
from hooks.classifier.definitions import Definition
from hooks.classifier.result import Answer


def _verdicts(definition: Definition, state: object, params: dict, answers: dict[str, Answer]) -> dict:
    needed, covered, size = (answers[key] for key in ("needed", "covered", "size"))
    high, low = definition.thresholds["yes"], definition.thresholds["no"]
    decision = None
    if needed.noul <= low or covered.noul >= high:
        decision = False
    elif needed.noul >= high and covered.noul <= low and (size.confidence or 0) >= high:
        decision = True
    return {"needs_documentation": decision, "size": size.choice}


RULE = code_rules.CodeRule(
    code_rules.asked,
    _verdicts,
    {"needs_documentation": (True, False, None), "size": ("none", "small", "medium", "large", None)},
    {"needs_documentation": False, "size": "none"},
)


def classify(evidence: dict, *, decider: Callable | None = None) -> dict:
    output = runner.run("documentation-needed", evidence, decider=decider)
    return {
        **output.verdicts,
        "source": output.raw.source,
        "answers": {key: value.to_dict() for key, value in output.raw.answers.items()},
    }
