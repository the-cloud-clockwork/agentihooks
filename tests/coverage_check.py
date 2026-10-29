"""Check that coverage data files are sound SQLite databases holding the tables reuse and combine read."""

import sqlite3
import sys
from contextlib import closing
from pathlib import Path

TABLES = frozenset({"arc", "context", "file", "line_bits"})


def problem(path: Path) -> str | None:
    if not path.is_file():
        return "missing"
    try:
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
            integrity = db.execute("pragma integrity_check").fetchone()[0]
            tables = {name for (name,) in db.execute("select name from sqlite_master where type = 'table'")}
    except sqlite3.DatabaseError as error:
        return str(error)
    if integrity != "ok":
        return integrity
    missing = TABLES - tables
    return f"missing tables {', '.join(sorted(missing))}" if missing else None


def main(argv: list[str] | None = None) -> int:
    status = 0
    for path in sys.argv[1:] if argv is None else argv:
        found = problem(Path(path))
        if found:
            print(f"::error::The coverage database {path} is unusable: {found}")
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
