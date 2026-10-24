"""Measures the intent check on its calibration corpus: fixed inputs, each answered several times before and after
a change to the questions or the rule, scored against independent labels. `python -m scripts.gates.intent_calibration`
prints the measurement."""

import json
import sys
from pathlib import Path

from hooks.classifier import corpus, definitions, evaluation
from scripts.gates import intent


def side(cases, outcomes):
    scored = evaluation.score(cases, list(outcomes))
    return {
        "samples": scored["samples"],
        "wrong": scored["wrong"],
        "wrong_cases": scored["wrong_cases"],
        "controls_rejected": scored["held_controls"],
    }


def measure(cases: tuple[corpus.Case, ...], definition: definitions.Definition | None = None) -> dict:
    definition = definitions.load(intent.PURPOSE) if definition is None else definition
    before = side(cases, evaluation.baseline(cases))
    after = side(cases, evaluation.replay(definition, cases))
    held = set(before["controls_rejected"]) <= set(after["controls_rejected"])
    return {
        "cases": len(cases),
        "controls": sum(case.control for case in cases),
        "before": before,
        "after": after,
        "calibrated": after["wrong"] < before["wrong"] and held,
    }


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    definition = definitions.load(intent.PURPOSE)
    path = Path(args[0]) if args else corpus.path_for(intent.PURPOSE)
    print(json.dumps(measure(corpus.load(definition, path)), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
