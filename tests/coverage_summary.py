"""Write and read the executed lines summary each unit shard uploads, so the coverage ratchet never downloads databases."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

from coverage import Coverage
from coverage.exceptions import NotPython
from coverage.python import PythonFileReporter

from tests.coverage_grade import GRADED, Unmeasured, graded_modules, measured_lines

NAME = "executed-lines.json"
STATEMENTS = "statement-lines.json"


def write(databases: list[Path], out: Path) -> None:
    """Record, per database, the lines it ran under the graded trees."""
    summary = {
        str(database): {path: sorted(ran) for path, ran in sorted(measured_lines(database).items())}
        for database in databases
    }
    out.write_text(json.dumps(summary, sort_keys=True))


def executed(summaries: list[Path]) -> dict[str, set[int]]:
    """Union the lines every summarized database ran, refusing a database that ran no graded line."""
    lines: dict[str, set[int]] = {}
    for summary in summaries:
        for database, measured in json.loads(summary.read_text()).items():
            if not measured:
                raise Unmeasured(f"shard {summary.parent / database} measured no line under {', '.join(GRADED)}")
            for path, ran in measured.items():
                lines.setdefault(path, set()).update(ran)
    return lines


def digest(source: bytes) -> str:
    """The source digest a statement summary records, so the ratchet reuses it only for the same module text."""
    return hashlib.sha256(source).hexdigest()


def write_statements(head: Path, part: int, parts: int, out: Path) -> None:
    """Record statement lines and multi line first lines for one slice of the graded modules, as the ratchet reads them."""
    coverage = Coverage(data_file=None, config_file=False)
    coverage.set_option("run:relative_files", True)
    found = {}
    for path in graded_modules(head)[part - 1 :: parts]:
        reporter = PythonFileReporter(str(head / path), coverage)
        try:
            statements = reporter.lines()
            first = reporter.multiline_map()
        except NotPython:
            continue
        found[path] = {
            "digest": digest((head / path).read_bytes()),
            "statements": sorted(statements),
            "first": {str(line): start for line, start in sorted(first.items()) if line != start},
        }
    out.write_text(json.dumps(found, sort_keys=True))


def statements(summaries: list[Path]) -> dict[str, dict]:
    """Union the statement summaries every shard wrote."""
    found: dict[str, dict] = {}
    for summary in summaries:
        found.update(json.loads(summary.read_text()))
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="summarize the lines coverage databases ran under the graded trees")
    parser.add_argument("databases", nargs="*", type=Path)
    parser.add_argument("--out", type=Path, default=Path(NAME))
    parser.add_argument("--statements", type=Path, help="also write this shard's slice of graded module statements")
    parser.add_argument("--slice", default="1/1", help="PART/PARTS: the slice of graded modules this shard reads")
    parser.add_argument("--head", type=Path, default=Path.cwd(), help="checkout whose modules are read")
    args = parser.parse_args(argv)
    if args.databases:
        write(args.databases, args.out)
    if args.statements:
        part, _, parts = args.slice.partition("/")
        write_statements(args.head, int(part), int(parts), args.statements)
    return 0


if __name__ == "__main__":
    sys.exit(main())
