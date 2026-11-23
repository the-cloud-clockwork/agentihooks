"""The lab evidence printer: a grade's kinds, unit keys, run ids, grader revision and object paths in a stable block.

record writes a grade's evidence once to lab/grades/<run>/grade.json and a head marker lab/heads/<head>/<run>;
python -m scripts.ci_lab evidence --grade RUN and agentihooks ci lab show SHA print it, exiting 1 on a missing object.
"""

import argparse
import json
import os
import sys
import time
from collections import Counter
from collections.abc import Sequence, Set
from dataclasses import asdict, dataclass
from pathlib import Path

from scripts.ci_lab import flakes
from scripts.ci_lab.digests import GradeInputs
from scripts.ci_lab.objects import ROOT, LocalObjects, Objects, grade_prefix, unit_path

__all__ = [
    "GradeEvidence",
    "GradeUnit",
    "ci",
    "grade_path",
    "head_prefix",
    "main",
    "record",
    "show",
]

GRADE = "grade.json"
HEADS = f"{ROOT}heads/"
NAMES = ("report", "log")


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


def grade_path(run: str) -> str:
    """Return the path of a grade run's evidence document."""
    return grade_prefix(run) + GRADE


def head_prefix(head: str) -> str:
    """Return the folder holding one marker per grade run of a head."""
    return f"{HEADS}{head}/"


def record(objects: Objects, evidence: GradeEvidence) -> None:
    """Write a grade's evidence and its head marker once; a later record of the same run keeps the first."""
    document = {
        "run": evidence.run,
        "inputs": asdict(evidence.inputs),
        "units": [asdict(unit) for unit in evidence.units],
    }
    objects.write_once(grade_path(evidence.run), json.dumps(document).encode())
    objects.write_once(head_prefix(evidence.inputs.head) + evidence.run, b"")


def _load(objects: Objects, run: str) -> GradeEvidence | None:
    path = grade_path(run)
    if path not in objects.keys(grade_prefix(run)):
        return None
    document = json.loads(objects.read(path))
    units = tuple(GradeUnit(**unit) for unit in document["units"])
    return GradeEvidence(run=document["run"], inputs=GradeInputs(**document["inputs"]), units=units)


def _missing(objects: Objects, units: Sequence[GradeUnit]) -> list[str]:
    paths = [unit_path(unit.kind, unit.key, name) for unit in units for name in NAMES]
    folders = dict.fromkeys(path.rsplit("/", 1)[0] + "/" for path in paths)
    present = {key for folder in folders for key in objects.keys(folder)}
    return [path for path in paths if path not in present]


def _render(evidence: GradeEvidence, units: Sequence[GradeUnit], missing: Set[str]) -> str:
    inputs = evidence.inputs
    kinds = Counter(unit.kind for unit in units)
    lines = [
        f"grade {evidence.run}",
        f"repository {inputs.repository}",
        f"head {inputs.head}",
        f"base {inputs.base}",
        f"tier {inputs.tier}",
        f"grader {inputs.grader}",
        " ".join(["kinds", *(f"{kind}:{kinds[kind]}" for kind in sorted(kinds))]),
    ]
    for unit in units:
        lines.append(f"unit {unit.kind} {unit.key} run {unit.run}")
        for name in NAMES:
            path = unit_path(unit.kind, unit.key, name)
            lines.append(f"  {name} {path}" + (" missing" if path in missing else ""))
    return "\n".join(lines) + "\n"


def show(objects: Objects, runs: Sequence[str]) -> tuple[str, list[str]]:
    """Return the evidence blocks of the runs, blank line separated, and every missing object path."""
    blocks: list[str] = []
    missing: list[str] = []
    for run in runs:
        evidence = _load(objects, run)
        if evidence is None:
            missing.append(grade_path(run))
            continue
        units = sorted(set(evidence.units), key=lambda unit: (unit.kind, unit.key, unit.run))
        absent = _missing(objects, units)
        blocks.append(_render(evidence, units, set(absent)))
        missing.extend(absent)
    return "\n".join(blocks), missing


def _emit(objects: Objects, runs: Sequence[str]) -> int:
    text, missing = show(objects, runs)
    sys.stdout.write(text)
    sys.stderr.writelines(f"missing {path}\n" for path in missing)
    return 1 if missing else 0


def main(argv: Sequence[str]) -> int:
    """Print one grade run's evidence: python -m scripts.ci_lab evidence --grade RUN --objects ROOT."""
    parser = argparse.ArgumentParser(prog="python -m scripts.ci_lab evidence")
    parser.add_argument("--grade", required=True)
    parser.add_argument("--objects", type=Path, required=True)
    arguments = parser.parse_args(argv)
    return _emit(LocalObjects(arguments.objects), [arguments.grade])


def ci(argv: Sequence[str]) -> int:
    """Run agentihooks ci lab show SHA --objects ROOT, printing a head's grades in run order, or ci lab flakes."""
    parser = argparse.ArgumentParser(prog="agentihooks ci")
    lab = parser.add_subparsers(dest="group", required=True).add_parser("lab")
    commands = lab.add_subparsers(dest="command", required=True)
    show_parser = commands.add_parser("show")
    show_parser.add_argument("sha")
    show_parser.add_argument("--objects", type=Path, required=True)
    flakes.declare(commands.add_parser("flakes"))
    arguments = parser.parse_args(argv)
    if arguments.command == "flakes":
        return flakes.command(arguments, os.environ, time.time())
    objects = LocalObjects(arguments.objects)
    prefix = head_prefix(arguments.sha)
    runs = sorted((path.removeprefix(prefix) for path in objects.keys(prefix)), key=lambda run: (len(run), run))
    if not runs:
        sys.stderr.write(f"missing {prefix}\n")
        return 1
    return _emit(objects, runs)
