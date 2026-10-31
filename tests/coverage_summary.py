"""Write and read the executed lines summary each unit shard uploads, so the coverage ratchet never downloads databases."""

import argparse
import json
import sys
from pathlib import Path

from tests.coverage_grade import GRADED, Unmeasured, measured_lines

NAME = "executed-lines.json"


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="summarize the lines coverage databases ran under the graded trees")
    parser.add_argument("databases", nargs="+", type=Path)
    parser.add_argument("--out", type=Path, default=Path(NAME))
    args = parser.parse_args(argv)
    write(args.databases, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
