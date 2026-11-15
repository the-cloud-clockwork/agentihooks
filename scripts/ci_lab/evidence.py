"""The lab evidence printer: a grade's kinds, unit keys, run ids, grader revision and object paths in a stable block.

record writes a grade's evidence once to lab/grades/<run>/grade.json and a head marker lab/heads/<head>/<run>;
python -m scripts.ci_lab evidence --grade RUN and agentihooks ci lab show SHA print it, exiting 1 on a missing object.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from scripts.ci_lab.digests import GradeInputs
from scripts.ci_lab.objects import Objects

__all__ = [
    "GradeEvidence",
    "GradeUnit",
    "ci",
    "main",
    "record",
]


@dataclass(frozen=True)
class GradeUnit:
    """One unit of a grade: its kind, its key and the run whose verdict the grade used."""

    kind: str
    key: str
    run: str


@dataclass(frozen=True)
class GradeEvidence:
    """One grade run: the inputs it graded and the units it folded."""

    run: str
    inputs: GradeInputs
    units: tuple[GradeUnit, ...]


def record(objects: Objects, evidence: GradeEvidence) -> None:
    """Write a grade's evidence and its head marker once; a later record of the same run keeps the first."""
    raise NotImplementedError


def main(argv: Sequence[str]) -> int:
    """Print one grade run's evidence: python -m scripts.ci_lab evidence --grade RUN --objects ROOT."""
    raise NotImplementedError


def ci(argv: list[str]) -> int:
    """Print every grade of a head: agentihooks ci lab show SHA --objects ROOT."""
    raise NotImplementedError
