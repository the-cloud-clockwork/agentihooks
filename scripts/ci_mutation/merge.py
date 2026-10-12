import argparse
import json
from pathlib import Path

LISTS = ("failures", "untouched_survivors", "cleared")


def merge_reports(reports: list[dict]) -> dict:
    files, not_mutated = {}, {}
    for report in reports:
        for entry in report["files"]:
            merged = files.setdefault(
                entry["path"], {"path": entry["path"], "counts": {}, **{key: [] for key in LISTS}}
            )
            for status, count in entry["counts"].items():
                merged["counts"][status] = merged["counts"].get(status, 0) + count
            for key in LISTS:
                merged[key] += entry[key]
        for entry in report["not_mutated"]:
            not_mutated.setdefault(entry["path"], entry)
    return {
        "files": list(files.values()),
        "not_mutated": list(not_mutated.values()),
        "failed": any(report["failed"] for report in reports),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    parser.add_argument("--shards", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    found = {shard: args.folder / f"mutation-preflight-shard-{shard}" / "report.json" for shard in range(args.shards)}
    missing = [str(shard) for shard, path in found.items() if not path.is_file()]
    merged = merge_reports([json.loads(path.read_text()) for path in found.values() if path.is_file()])
    merged["failed"] = merged["failed"] or bool(missing)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(json.dumps(merged) + "\n")
    print(f"Merged {args.shards - len(missing)} of {args.shards} mutation shard reports")
    if missing:
        print(f"missing mutation shard reports: {', '.join(missing)}")
    return int(merged["failed"])


if __name__ == "__main__":
    raise SystemExit(main())
