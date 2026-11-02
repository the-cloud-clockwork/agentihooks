"""Grades a mutation shard report with this copy's grader, reading clearances from the graded tree."""

import argparse
import json
from pathlib import Path

from scripts.ci_mutation.clearances import load_clearances
from scripts.ci_mutation.report import evaluate, survivor_text
from scripts.ci_mutation.scope import discover_changes, graded_green, own_bases


def regrade(report: dict, cleared: dict, changes: dict[str, set[int]]) -> dict:
    """Return the report with every changed file graded again from its recorded rows."""
    graded = report.get("graded", [])
    if sorted(entry["path"] for entry in graded) != sorted(file["path"] for file in report["files"]):
        raise ValueError("Mutation report rows do not cover exactly its graded files")
    if sorted(file["path"] for file in report["files"] + report["not_mutated"]) != sorted(changes):
        raise ValueError("Mutation report does not cover exactly the changed Python files")
    files = [evaluate(entry["path"], entry["rows"], changes[entry["path"]], cleared) for entry in graded]
    return {**report, "files": files, "failed": bool(report["not_mutated"]) or any(file["failures"] for file in files)}


def bases_held(root: Path, base: str, claimed: list[str]) -> bool:
    """Return whether the claimed bases start at the merge base and add only ones this grader graded green."""
    chosen = own_bases(root, base, "HEAD", graded_green)
    return claimed[:1] == chosen[:1] and set(claimed) <= set(chosen)


def main(argv: list[str] | None = None) -> int:
    """Hold the planned bases, or grade the report in place, and return 1 when either fails."""
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command")
    held = commands.add_parser("bases")
    held.add_argument("root", type=Path)
    held.add_argument("base")
    held.add_argument("bases")
    grade = commands.add_parser("report")
    grade.add_argument("report", type=Path)
    grade.add_argument("root", type=Path)
    grade.add_argument("bases")
    args = parser.parse_args(argv)
    if args.command == "bases":
        if bases_held(args.root, args.base, args.bases.split(",")):
            return 0
        print(f"Protected grader: the planned mutation bases {args.bases} are not ones it would choose")
        return 1
    changes = discover_changes(args.root, args.bases.split(","), "HEAD")
    graded = regrade(json.loads(args.report.read_text()), load_clearances(args.root), changes)
    args.report.write_text(json.dumps(graded) + "\n")
    for file in graded["files"]:
        if file["failures"]:
            print(survivor_text(file))
    print(f"Protected grader: mutation report {'failed' if graded['failed'] else 'passed'}")
    return int(graded["failed"])


if __name__ == "__main__":
    raise SystemExit(main())
