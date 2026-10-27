"""Keep a dev shard's coverage of the tests this run reused, moved onto this run's source lines.

A reused test executed only units whose text did not change, so its arcs survive in the head source at the line the
unchanged text moved to. Import time arcs carry the empty context and are kept the same way. Page captures name the
test that took them; captures taken outside a test, or recorded before captures named one, are kept whole.
"""

import argparse
import difflib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from tests import coverage_check
from tests.dep_manifest import node


def line_map(old: str, new: str) -> dict[int, int]:
    matcher = difflib.SequenceMatcher(None, old.split("\n"), new.split("\n"), autojunk=False)
    return {
        start + offset + 1: target + offset + 1
        for start, target, size in matcher.get_matching_blocks()
        for offset in range(size)
    }


def _moved(number: int, lines: dict[int, int]) -> int | None:
    target = lines.get(abs(number))
    return None if target is None else (target if number > 0 else -target)


def keep(source: Path, out: Path, tests: set[str], moved: dict[str, dict[int, int] | None]) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, out)
    with sqlite3.connect(out) as db:
        contexts = db.execute("select id, context from context").fetchall()
        kept = [(row,) for row, context in contexts if not context or node(context.rsplit("|", 1)[0]) in tests]
        db.execute("create temp table kept (id integer primary key)")
        db.executemany("insert into kept values (?)", kept)
        for table in ("arc", "line_bits"):
            db.execute(f"delete from {table} where context_id not in (select id from kept)")
        db.execute("delete from context where id not in (select id from kept)")
        files = dict(db.execute("select path, id from file"))
        for path, lines in moved.items():
            if path not in files:
                continue
            arcs = db.execute("select context_id, fromno, tono from arc where file_id = ?", (files[path],)).fetchall()
            db.execute("delete from arc where file_id = ?", (files[path],))
            db.execute("delete from line_bits where file_id = ?", (files[path],))
            if lines is None:
                db.execute("delete from file where id = ?", (files[path],))
                continue
            shifted = [
                (files[path], context, start, end)
                for context, start, end in (
                    (context, _moved(start, lines), _moved(end, lines)) for context, start, end in arcs
                )
                if start is not None and end is not None
            ]
            db.executemany("insert or ignore into arc (file_id, context_id, fromno, tono) values (?, ?, ?, ?)", shifted)
    return len({node(context.rsplit("|", 1)[0]) for _, context in contexts if context} & tests)


def keep_captures(source: Path, out: Path, tests: set[str]) -> int:
    count = 0
    for capture in sorted(source.glob("capture-*.json")):
        recorded = json.loads(capture.read_text(encoding="utf-8"))
        test = recorded.get("test")
        if test and node(test.rsplit(" (", 1)[0]) not in tests:
            continue
        (out / "sources").mkdir(parents=True, exist_ok=True)
        for entry in recorded["result"]:
            shutil.copyfile(source / "sources" / f"{entry['source']}.js", out / "sources" / f"{entry['source']}.js")
        shutil.copyfile(capture, out / capture.name)
        count += 1
    return count


def _show(repo: Path, revision: str, path: str) -> str | None:
    shown = subprocess.run(["git", "-C", str(repo), "show", f"{revision}:{path}"], capture_output=True)
    return shown.stdout.decode("utf-8", "replace") if shown.returncode == 0 else None


def moved_lines(repo: Path, commit: str, head: str) -> dict[str, dict[int, int] | None]:
    # In a partial clone --numstat fetches the missing blobs in one batch; --name-only fetches none.
    command = ["git", "-C", str(repo), "diff", "--no-renames", "--numstat", commit, head, "--", "*.py"]
    moved = {}
    for line in subprocess.run(command, check=True, capture_output=True, text=True).stdout.splitlines():
        path = line.split("\t", 2)[2]
        old, new = _show(repo, commit, path), _show(repo, head, path)
        if old is not None:
            moved[path] = None if new is None else line_map(old, new)
    return moved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--coverage", type=Path, required=True, help="the dev shard's coverage data file")
    parser.add_argument("--commit", required=True, help="the dev commit the coverage was measured on")
    parser.add_argument("--reused", type=Path, required=True, help="the tests this run deselected")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--captures", type=Path, required=True, help="the dev shard's page capture folder")
    parser.add_argument("--captures-out", type=Path, required=True)
    args = parser.parse_args(argv)
    unusable = coverage_check.problem(args.coverage)
    if unusable:
        artifact = os.environ.get("ARTIFACT", str(args.coverage))
        print(f"::error::The dev coverage {artifact} from {args.commit} is unusable ({unusable})")
        return 1
    deselected = json.loads(args.reused.read_text())
    tests = set(deselected["tests"]) - set(deselected.get("branch", []))
    moved = moved_lines(args.repo, args.commit, args.head)
    count = keep(args.coverage, args.out, tests, moved)
    captures = keep_captures(args.captures, args.captures_out, tests)
    print(
        f"Kept the dev coverage of {count} reused tests and {captures} page captures from {args.commit};"
        f" {len(moved)} changed files remapped"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
