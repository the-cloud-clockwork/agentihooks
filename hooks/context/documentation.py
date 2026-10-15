from collections.abc import Callable

from hooks.classifier import runner


def classify(evidence: dict, *, decider: Callable | None = None) -> dict:
    output = runner.run("documentation-needed", evidence, decider=decider)
    answers = output.raw.answers
    needed, covered, size = (answers[key] for key in ("needed", "covered", "size"))
    high, low = output.thresholds["yes"], output.thresholds["no"]
    decision = None
    if needed.noul <= low or covered.noul >= high:
        decision = False
    elif needed.noul >= high and covered.noul <= low and (size.confidence or 0) >= high:
        decision = True
    return {
        "needs_documentation": decision,
        "size": size.choice,
        "source": output.raw.source,
        "answers": {key: value.to_dict() for key, value in answers.items()},
    }
