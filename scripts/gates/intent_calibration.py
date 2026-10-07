"""Measures the intent check on its calibration corpus: fixed inputs, each answered several times before and after
a change to the questions or the rule, scored against independent labels. `python -m scripts.gates.intent_calibration`
prints the measurement."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from scripts.gates import intent

CORPUS = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "intent_calibration.json"


def recorded(answers):
    return lambda state, questions, *, purpose: SimpleNamespace(
        answers={name: SimpleNamespace(noul=value) for name, value in answers.items()}
    )


def verdicts(case, side):
    if side == "before":
        return [sample["verdict"] for sample in case["samples"]["before"]]
    return [intent.judge(case["state"], decide=recorded(sample["answers"]))[0] for sample in case["samples"]["after"]]


def side_of(corpus, side):
    judged = {case["id"]: verdicts(case, side) for case in corpus["cases"]}
    expected = {case["id"]: case["expected"] for case in corpus["cases"]}
    controls = [case["id"] for case in corpus["cases"] if case["control"]]
    return {
        "samples": sum(len(found) for found in judged.values()),
        "wrong": sum(verdict != expected[case] for case, found in judged.items() for verdict in found),
        "wrong_cases": sorted(case for case, found in judged.items() if any(v != expected[case] for v in found)),
        "controls_rejected": sorted(case for case in controls if set(judged[case]) == {intent.FAIL}),
    }


def measure(corpus):
    before, after = side_of(corpus, "before"), side_of(corpus, "after")
    held = set(before["controls_rejected"]) <= set(after["controls_rejected"])
    return {
        "cases": len(corpus["cases"]),
        "controls": sum(case["control"] for case in corpus["cases"]),
        "before": before,
        "after": after,
        "calibrated": after["wrong"] < before["wrong"] and held,
    }


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    path = Path(args[0]) if args else CORPUS
    print(json.dumps(measure(json.loads(path.read_text())), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
