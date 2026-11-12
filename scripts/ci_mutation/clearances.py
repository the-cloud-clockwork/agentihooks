import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


def clearance_path(root: Path, key: str) -> Path:
    return root / "mutation-clearances" / f"{hashlib.sha256(key.encode()).hexdigest()}.json"


def load_clearances(root: Path) -> dict:
    legacy = root / "mutation-cleared.txt"
    cleared = json.loads(legacy.read_text()) if legacy.exists() else {}
    for path in sorted((root / "mutation-clearances").glob("*.json")):
        record = json.loads(path.read_text())
        if len(record) != 1:
            raise ValueError("Mutation clearance file requires one mutant identity")
        key, ruling = next(iter(record.items()))
        if path != clearance_path(root, key):
            raise ValueError("Mutation clearance filename does not match its identity")
        if key in cleared and cleared[key] != ruling:
            raise ValueError(f"Conflicting mutation clearance: {key}")
        cleared[key] = ruling
    return cleared


def write_clearance(root: Path, key: str, ruling: dict) -> None:
    target = clearance_path(root, key)
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps({key: ruling}, indent=2) + "\n")


def migrate(root: Path) -> int:
    load_clearances(root)
    legacy = root / "mutation-cleared.txt"
    records = json.loads(legacy.read_text())
    for key, ruling in records.items():
        write_clearance(root, key, ruling)
    legacy.write_text("{}\n")
    return len(records)


def report_keys(report: dict[str, Any], names: list[str]) -> list[str]:
    """Return the clearance key of each named survivor in a merged or per shard mutation report."""
    from .report import clearance_key  # pyright: ignore[reportUnknownVariableType]

    files: list[dict[str, Any]] = (
        report["files"] if "files" in report else [{"path": path, "failures": rows} for path, rows in report.items()]
    )
    keys = {
        row["name"]: clearance_key(entry["path"], row)
        for entry in files
        for row in entry["failures"]
        if row["status"] in {"survived", "no tests"}
    }
    missing = [name for name in names if name not in keys]
    if missing:
        raise ValueError(f"Mutants not surviving in the report: {', '.join(missing)}")
    return [keys[name] for name in names]


def main(argv: list[str] | None = None) -> int:
    """Migrate legacy rulings, or write one reader ruling per named survivor of a mutation report."""
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print(f"Migrated {migrate(Path.cwd())} mutation clearances")
        return 0
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["write"])
    parser.add_argument("report", type=Path)
    parser.add_argument("names", nargs="+")
    parser.add_argument("--reader", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parsed = parser.parse_args(args)
    for key in report_keys(json.loads(parsed.report.read_text()), parsed.names):
        write_clearance(parsed.root, key, {"reader": parsed.reader, "reason": parsed.reason})
        print(clearance_path(parsed.root, key).as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
