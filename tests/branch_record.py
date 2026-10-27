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
OUTCOMES = {"started", *RANK}
WORKER = re.compile(r"\.pid(\d+)\.")


def _text(value) -> bool:
    return isinstance(value, str)


def _texts(value) -> bool:
    return isinstance(value, list) and all(map(_text, value))


def _pid(value) -> bool:
    return type(value) is int


def _flag(value) -> bool:
    return type(value) is bool


def _known(value) -> bool:
    return isinstance(value, str) and value in OUTCOMES


def _ranked(value) -> bool:
    return isinstance(value, str) and value in RANK


RESULT = {"test": _text, "outcome": _known, "pid": _pid}
FLAKE = {"test": _text, "outcome": _ranked}
INPUT = {"test": _text, "files": _texts, "escapes": _flag}


def _row(line: str, fields: dict) -> dict | None:
    try:
        row = json.loads(line)
    except ValueError:
        return None
    if isinstance(row, dict) and all(check(row.get(key)) for key, check in fields.items()):
        return row
    return None


def _rows(paths: list[Path], fields: dict) -> tuple[list[dict], bool]:
    rows, readable = [], True
    for path in paths:
        try:
            *lines, tail = path.read_text().split("\n")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeDecodeError):
            readable = False
            continue
        found = [_row(line, fields) for line in lines]
        readable = readable and None not in found
        # A writer killed mid line leaves only its last line cut short; any other unreadable line is damage.
        rows += [row for row in [*found, _row(tail, fields) if tail else None] if row is not None]
    return rows, readable


def _rank(outcomes: dict[str, str], test: str, outcome: str) -> None:
    if RANK[outcome] > RANK.get(outcomes.get(test), -1):
        outcomes[test] = outcome


def _outcomes(rows: list[dict]) -> tuple[dict[str, str], dict[str, set[int]]]:
    outcomes, pids, started, ended = {}, {}, set(), set()
    for row in rows:
        test, outcome, pid = dep_manifest.node(row["test"]), row["outcome"], row.get("pid")
        if outcome == "started":
            started.add((test, pid))
            continue
        ended.add((test, pid))
        _rank(outcomes, test, outcome)
        if outcome == "passed":
            pids.setdefault(test, set()).add(pid)
    for test, _ in started - ended:
        _rank(outcomes, test, "failed")
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
    results, readable = _rows(sorted(args.results.glob("*.jsonl")), RESULT)
    flakes, flakes_readable = _rows([args.flakes], FLAKE)
    outcomes, pids = _outcomes(results + flakes)
    inputs = _rows(sorted(args.inputs.rglob("*.jsonl")), INPUT)[0]
    with tempfile.TemporaryDirectory() as scratch:
        coverage, surviving, state = _coverage(args.coverage_dir, Path(scratch))
        manifest = _manifest(coverage, inputs, args, Path(scratch))
    provable = manifest is not None and readable and flakes_readable
    measured = {dep_manifest.node(row["test"]) for row in inputs} if provable else set()
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
    damage = "" if readable and flakes_readable else "; a damaged results or flake line keeps every pass out"
    note = (
        f"Branch record for leg {args.leg}: kept {len(passed)} of {len(candidates)} passed tests with their dependencies,"
        f" {len(found['failed'])} failed, {len(found['flaky'])} flaky; coverage {state}{damage}"
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
