"""Build one leg's branch record: its dependency manifest and the tests it passed, failed and passed only on retry.

A cancelled or killed leg leaves no combined coverage, so a passed test counts only when the coverage file of the
worker that ran it is intact and holds per test contexts, and the test's dependency input line was written.
"""

import argparse
import json
import re
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

from tests import dep_manifest

RANK = {"skipped": 0, "passed": 1, "flaky": 2, "failed": 3}
WORKER = re.compile(r"\.pid(\d+)\.")
RESULT = {"test": str, "outcome": str}
INPUT = {"test": str, "files": list, "escapes": bool}


def _text(value) -> bool:
    return isinstance(value, str)


def _rows(paths: list[Path], fields: dict[str, type]) -> list[dict]:
    rows = []
    for path in paths:
        try:
            lines = path.read_text().splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and all(isinstance(row.get(key), kind) for key, kind in fields.items()):
                rows.append(row)
    return rows


def _outcomes(rows: list[dict]) -> tuple[dict[str, str], dict[str, set[int]]]:
    outcomes, pids = {}, {}
    for row in rows:
        test, outcome = dep_manifest.node(row["test"]), row["outcome"]
        if outcome not in RANK:
            continue
        if RANK[outcome] > RANK.get(outcomes.get(test), -1):
            outcomes[test] = outcome
        if outcome == "passed" and isinstance(row.get("pid"), int):
            pids.setdefault(test, set()).add(row["pid"])
    return outcomes, pids


def _intact(path: Path) -> bool:
    try:
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
            if db.execute("pragma integrity_check").fetchall() != [("ok",)]:
                return False
            return db.execute("select 1 from context where context like '%|%' limit 1").fetchone() is not None
    except (OSError, sqlite3.DatabaseError):
        return False


def _coverage(folder: Path, scratch: Path) -> tuple[Path, set[int] | None, str]:
    combined = folder / ".coverage"
    if (folder / "elapsed.json").is_file() and _intact(combined):
        return combined, None, "complete"
    from coverage import CoverageData
    from coverage.exceptions import CoverageException

    data = CoverageData(basename=str(scratch / ".coverage"))
    pids = set()
    for path in sorted(folder.glob(".coverage.*")):
        worker = WORKER.search(path.name)
        if not worker or not _intact(path):
            continue
        part = CoverageData(basename=str(path))
        try:
            part.read()
            data.update(part)
        except (CoverageException, sqlite3.DatabaseError):
            continue
        pids.add(int(worker.group(1)))
    return scratch / ".coverage", pids, f"partial, {len(pids)} worker files"


def _manifest(coverage: Path, inputs: list[dict], args: argparse.Namespace, scratch: Path) -> dict | None:
    from coverage.exceptions import CoverageException

    lines = scratch / "inputs.jsonl"
    lines.write_text("".join(json.dumps(row) + "\n" for row in inputs))
    try:
        manifest = dep_manifest.build(coverage, [lines], args.root.resolve(), args.commit, args.environment)
    except (CoverageException, sqlite3.DatabaseError, OSError, ValueError):
        return None
    manifest.pop("lines")
    return manifest


def record(args: argparse.Namespace) -> tuple[dict, str]:
    outcomes, pids = _outcomes(_rows(sorted(args.results.glob("*.jsonl")), RESULT) + _rows([args.flakes], RESULT))
    inputs = [row for row in _rows(sorted(args.inputs.rglob("*.jsonl")), INPUT) if all(map(_text, row["files"]))]
    with tempfile.TemporaryDirectory() as scratch:
        coverage, surviving, state = _coverage(args.coverage_dir, Path(scratch))
        manifest = _manifest(coverage, inputs, args, Path(scratch))
    measured = {dep_manifest.node(row["test"]) for row in inputs} if manifest else set()
    candidates = sorted(test for test, outcome in outcomes.items() if outcome == "passed")
    passed = [
        test for test in candidates if test in measured and (surviving is None or pids.get(test, set()) & surviving)
    ]
    found = {
        "version": 1,
        "run": args.run,
        "attempt": args.attempt,
        "leg": args.leg,
        "commit": args.commit,
        "head": args.head,
        "pull": args.pull,
        "environment": args.environment,
        "manifest": manifest
        or {"version": 2, "commit": args.commit, "environment": args.environment, "units": [], "tests": {}},
        "passed": passed,
        "failed": sorted(test for test, outcome in outcomes.items() if outcome == "failed"),
        "flaky": sorted(test for test, outcome in outcomes.items() if outcome == "flaky"),
    }
    note = (
        f"Branch record for leg {args.leg}: kept {len(passed)} of {len(candidates)} passed tests with their dependencies,"
        f" {len(found['failed'])} failed, {len(found['flaky'])} flaky; coverage {state}"
    )
    return found, note


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    for name in ("--coverage-dir", "--inputs", "--results", "--flakes", "--out"):
        parser.add_argument(name, type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    for name in ("--commit", "--head", "--leg", "--environment"):
        parser.add_argument(name, required=True)
    for name in ("--pull", "--run", "--attempt"):
        parser.add_argument(name, type=int, required=True)
    args = parser.parse_args(argv)
    found, note = record(args)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(found, separators=(",", ":")))
    print(note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
