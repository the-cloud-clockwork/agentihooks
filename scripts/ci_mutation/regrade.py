"""Grades a mutation shard report with this copy's grader, reading clearances from the graded tree."""

import argparse
import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from scripts.ci_mutation.clearances import load_clearances  # pyright: ignore[reportUnknownVariableType]
from scripts.ci_mutation.merge import merge_units  # pyright: ignore[reportUnknownVariableType]
from scripts.ci_mutation.mutant_shards import split_names  # pyright: ignore[reportUnknownVariableType]
from scripts.ci_mutation.report import evaluate, survivor_text  # pyright: ignore[reportUnknownVariableType]
from scripts.ci_mutation.scope import (
    discover_changes,
    graded_green,
    own_bases,  # pyright: ignore[reportUnknownVariableType]
)

GRADER = Path(__file__).resolve().parents[2]


def regrade(report: dict[str, Any], cleared: dict[str, Any], changes: dict[str, set[int]]) -> dict[str, Any]:
    """Return the report with every changed file graded again from its recorded rows."""
    graded: list[dict[str, Any]] = report.get("graded", [])
    if sorted(entry["path"] for entry in graded) != sorted(file["path"] for file in report["files"]):
        raise ValueError("Mutation report rows do not cover exactly its graded files")
    if sorted(file["path"] for file in report["files"] + report["not_mutated"]) != sorted(changes):
        raise ValueError("Mutation report does not cover exactly the changed Python files")
    files: list[dict[str, Any]] = [
        evaluate(entry["path"], entry["rows"], changes[entry["path"]], cleared) for entry in graded
    ]
    return {**report, "files": files, "failed": bool(report["not_mutated"]) or any(file["failures"] for file in files)}


def has_functions(source: Path) -> bool:
    """Return whether the file defines a function the runner would mutate."""
    return source.is_file() and any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) for node in ast.walk(ast.parse(source.read_text()))
    )


def recollect(output: Path, wanted: set[str], shard: tuple[int, int]) -> dict[str, list[dict[str, Any]]]:
    """Return the rows this copy's collector reads from each work folder's mutmut records for the wanted files."""
    rows = {}
    for listing in sorted(output.glob("*/changed-lines.json")):
        paths = sorted(wanted.intersection(json.loads(listing.read_text())))
        if not paths:
            continue
        folder = listing.parent.resolve()
        target = folder / "protected-results.json"
        command = [sys.executable, "-m", "scripts.ci_mutation.report", str(target), *map(str, shard), *paths]
        env = {**os.environ, "PYTHONPATH": str(GRADER), "PYTHONSAFEPATH": "1"}
        subprocess.run(command, cwd=folder, env=env, check=True)
        for path, found in json.loads(target.read_text()).items():
            rows.setdefault(path, []).extend(found)
    return rows


def rebuild(
    report: dict[str, Any],
    rows: dict[str, list[dict[str, Any]]],
    split: set[str],
    changes: dict[str, set[int]],
    root: Path,
) -> dict[str, Any]:
    """Return the report with its graded rows and split units taken from the protected rows."""
    graded, units = [], []
    for entry in report.get("graded", []):
        path = entry["path"]
        if path not in rows and has_functions(root / path):
            raise ValueError(f"Protected collector read no mutation rows for {path}")
        found = rows.get(path, [])
        lines = sorted(changes.get(path, ()))
        graded.append({"path": path, "lines": lines, "rows": [row for row in found if row["name"] not in split]})
        if picked := [row for row in found if row["name"] in split]:
            units.append({"path": path, "lines": lines, "rows": picked})
    return {**report, "graded": graded, "units": units}


def grade_units(folder: Path, shards: int, root: Path, bases: list[str]) -> int:
    """Grade every shard's split units together with this copy's merge and return 1 when one fails."""
    found = [folder / f"mutation-report-{shard}" / "report.json" for shard in range(shards)]
    if missing := [path.as_posix() for path in found if not path.is_file()]:
        print(f"Protected grader: missing mutation shard reports {', '.join(missing)}")
        return 1
    changes = discover_changes(root, bases, "HEAD")
    reports = [json.loads(path.read_text()) for path in found]
    for report in reports:
        for entry in report.get("units", []):
            entry["lines"] = sorted(changes.get(entry["path"], ()))
    units = merge_units(reports, load_clearances(root))
    for unit in units:
        if unit["failures"]:
            print(survivor_text(unit))
    failed = any(unit["failures"] for unit in units)
    print(f"Protected grader: split mutation units {'failed' if failed else 'passed'}")
    return int(failed)


def main(argv: list[str] | None = None) -> int:
    """Print the protected bases, grade a shard report in place, or grade the split units, returning 1 on a failure."""
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command")
    held = commands.add_parser("bases")
    held.add_argument("root", type=Path)
    held.add_argument("base")
    grade = commands.add_parser("report")
    grade.add_argument("report", type=Path)
    grade.add_argument("root", type=Path)
    grade.add_argument("bases")
    grade.add_argument("--shard", type=int, default=0)
    grade.add_argument("--shards", type=int, default=1)
    grade.add_argument("--stats", type=Path)
    units = commands.add_parser("units")
    units.add_argument("folder", type=Path)
    units.add_argument("shards", type=int)
    units.add_argument("root", type=Path)
    units.add_argument("bases")
    args = parser.parse_args(argv)
    if args.command == "bases":
        print(f"bases={','.join(own_bases(args.root, args.base, 'HEAD', graded_green))}")
        return 0
    if args.command == "units":
        return grade_units(args.folder, args.shards, args.root, args.bases.split(","))
    changes = discover_changes(args.root, args.bases.split(","), "HEAD")
    cleared: dict[str, Any] = load_clearances(args.root)  # pyright: ignore[reportUnknownVariableType]
    report = json.loads(args.report.read_text())
    wanted = {entry["path"] for entry in report.get("graded", [])}
    rows = recollect(args.report.parent, wanted, (args.shard, args.shards))
    plan = args.stats / "plan.json" if args.stats else None
    split = split_names(json.loads(plan.read_text())) if plan and plan.is_file() else set()
    graded = regrade(rebuild(report, rows, split, changes, args.root), cleared, changes)
    whole = merge_units([graded], cleared) if args.shards == 1 else []
    graded["failed"] = graded["failed"] or any(unit["failures"] for unit in whole)
    args.report.write_text(json.dumps(graded) + "\n")
    for file in graded["files"] + whole:
        if file["failures"]:
            print(survivor_text(file))
    print(f"Protected grader: mutation report {'failed' if graded['failed'] else 'passed'}")
    return int(graded["failed"])


if __name__ == "__main__":
    raise SystemExit(main())
