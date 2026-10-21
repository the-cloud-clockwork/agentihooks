import argparse
import json
from pathlib import Path

from scripts.ci_mutation.clearances import load_clearances
from scripts.ci_mutation.report import evaluate, survivor_text

LISTS = ("failures", "untouched_survivors", "cleared")


def combined_grade(rows: list[dict]) -> dict:
    # Each unit runs the shared head plus one slice and together they cover every test, so one kill kills the mutant.
    return next(
        (row for row in rows if row["status"] == "killed"),
        next((row for row in rows if row["status"] != "survived"), rows[0]),
    )


def merge_units(reports: list[dict], cleared: dict) -> list[dict]:
    units = {}
    for report in reports:
        for entry in report.get("units", []):
            unit = units.setdefault(entry["path"], {"lines": set(entry["lines"]), "rows": {}})
            for row in entry["rows"]:
                unit["rows"].setdefault(row["name"], []).append(row)
    return [
        evaluate(path, [combined_grade(rows) for _, rows in sorted(unit["rows"].items())], unit["lines"], cleared)
        for path, unit in sorted(units.items())
    ]


def merge_reports(reports: list[dict], cleared: dict | None = None) -> dict:
    files, not_mutated = {}, {}
    graded = merge_units(reports, cleared or {})
    for entry in [entry for report in reports for entry in report["files"]] + graded:
        merged = files.setdefault(entry["path"], {"path": entry["path"], "counts": {}, **{key: [] for key in LISTS}})
        for status, count in entry["counts"].items():
            merged["counts"][status] = merged["counts"].get(status, 0) + count
        for key in LISTS:
            merged[key] += entry[key]
    for report in reports:
        for entry in report["not_mutated"]:
            not_mutated.setdefault(entry["path"], entry)
    measurements = [report["shard"] for report in reports if "shard" in report]
    return {
        **({"shards": measurements} if measurements else {}),
        "files": list(files.values()),
        "not_mutated": list(not_mutated.values()),
        "failed": any(report["failed"] for report in reports) or any(entry["failures"] for entry in graded),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    parser.add_argument("--shards", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    found = {shard: args.folder / f"mutation-preflight-shard-{shard}" / "report.json" for shard in range(args.shards)}
    missing = [str(shard) for shard, path in found.items() if not path.is_file()]
    reports = [json.loads(path.read_text()) for path in found.values() if path.is_file()]
    cleared = load_clearances(Path.cwd()) if any(report.get("units") for report in reports) else {}
    merged = merge_reports(reports, cleared)
    merged["failed"] = merged["failed"] or bool(missing)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(json.dumps(merged) + "\n")
    print(f"Merged {args.shards - len(missing)} of {args.shards} mutation shard reports")
    if missing:
        print(f"missing mutation shard reports: {', '.join(missing)}")
    for entry in merge_units(reports, cleared):
        if entry["failures"]:
            print(survivor_text(entry))
    return int(merged["failed"])


if __name__ == "__main__":
    raise SystemExit(main())
