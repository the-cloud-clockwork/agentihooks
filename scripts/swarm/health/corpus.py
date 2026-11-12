"""Health detector corpus: recorded agent histories labelled with the finding each detector must or must not raise.

Each corpus file names one detector and its cases; a replay feeds every history through the health findings the
way swarm status does and reports each case whose outcome differs from its label.
"""

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.swarm.health import findings as health

DETECTORS = ("ceremony", "scope inflation", "proof loop", "idle with claim", "stale claim", "over monitoring")
LABELS = ("finding", "none")


@dataclass(frozen=True)
class Case:
    """One labelled history of the corpus."""

    detector: str
    name: str
    label: str
    history: dict[str, Any]


def load(directory: Path) -> list[Case]:
    """Every case of every corpus file in the directory, in file name order."""
    cases = []
    for path in sorted(directory.glob("*.json")):
        corpus = json.loads(path.read_text())
        for case in corpus["cases"]:
            if case["label"] not in LABELS:
                raise ValueError(f"{path.name}: {case['name']} is labelled {case['label']}, not finding or none")
            cases.append(Case(corpus["detector"], case["name"], case["label"], case["history"]))
    return cases


def outcome(case: Case, limits: health.Limits) -> str:
    """Whether the case's detector raises a finding on its history: finding or none."""
    history = case.history
    ledger = {
        "tasks": history.get("tasks", []),
        "_meta": {"events": history.get("events", [])},
        "review_rounds": history.get("review_rounds", {}),
    }
    found = health.findings(
        ledger,
        history.get("agents", []),
        history.get("activity", {}),
        limits,
        frozenset(history.get("waiting", [])),
        frozenset(history.get("green", [])),
        history.get("talk"),
    )
    return "finding" if any(f.kind == case.detector for f in found) else "none"


def flips(cases: list[Case], limits: health.Limits) -> list[Case]:
    """The cases whose replayed outcome differs from their label."""
    return [case for case in cases if outcome(case, limits) != case.label]


def unproven(cases: list[Case]) -> list[str]:
    """The detectors that lack a finding case or a none case."""
    labels = {(case.detector, case.label) for case in cases}
    return [d for d in DETECTORS if not all((d, label) in labels for label in LABELS)]


def main(argv: list[str]) -> int:
    """Replay the corpus directory given; exit 1 when a case flips or a detector is not proven both ways."""
    cases = load(Path(argv[0]))
    flipped, missing = flips(cases, health.Limits()), unproven(cases)
    for case in flipped:
        print(f"flipped  {case.detector}  {case.name}: labelled {case.label}")
    for detector in missing:
        print(f"unproven  {detector}: needs a finding case and a none case")
    print(f"{len(cases)} cases, {len(flipped)} flipped, {len(missing)} detectors unproven")
    return 1 if flipped or missing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
