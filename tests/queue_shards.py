import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from tests.shard_budget import step_seconds
from tests.shards import (
    FIRST_SHARD_FILES,
    SECONDS_PER_SOURCE_BYTE,
    assign_nodes,
    discover_test_files,
    grouped_files,
    source_sizes,
)

WORKERS = 4
STEP_SECONDS = step_seconds("unit")
STARTUP_SECONDS = 30
SLOWDOWN = 1.15
MAX_SHARDS = 30


def matrix(root: Path, versions: dict[str, dict[str, float]], counts: dict[str, int] | None = None) -> list[dict]:
    files = discover_test_files(root)
    grouped = grouped_files(root, files)
    sizes = source_sizes(root, files)
    result = []
    target = min(240, STEP_SECONDS / 2)
    for version, durations in versions.items():
        count = counts[version] if counts else 12
        while True:
            predicted = _predicted(durations, count, grouped, sizes)
            if counts or predicted < target or count == MAX_SHARDS:
                break
            count += 1
        print(f"Python {version}: {count} queue shards, predicted slowest {predicted:.1f}s of {target:.0f}s")
        result.extend(
            {"python-version": version, "shard": shard, "total": count, "predicted_seconds": predicted}
            for shard in range(1, count + 1)
        )
    return result


def _predicted(durations: dict[str, float], count: int, grouped: dict[str, str], sizes: dict[str, int]) -> float:
    measured = {node: seconds for node, seconds in durations.items() if node.split("::", 1)[0] in sizes}
    pinned = {node: seconds for node, seconds in measured.items() if node.split("::", 1)[0] in FIRST_SHARD_FILES}
    measured = {node: seconds for node, seconds in measured.items() if node not in pinned}
    parts = assign_nodes(measured, count, grouped, WORKERS, sizes=sizes)
    parts[0].extend(pinned)
    peak = max(_seconds(part, {**measured, **pinned}, grouped, sizes) for part in parts)
    return STARTUP_SECONDS + SLOWDOWN * peak


def _seconds(part: list[str], durations: dict[str, float], grouped: dict[str, str], sizes: dict[str, int]) -> float:
    serial = {}
    files = {node.split("::", 1)[0] for node in part}
    for node in part:
        if group := grouped.get(node.split("::", 1)[0]):
            serial[group] = serial.get(group, 0.0) + durations[node]
    return max(
        sum(durations[node] for node in part) / WORKERS,
        max((durations[node] for node in part), default=0.0),
        max(serial.values(), default=0.0),
    ) + sum(sizes[path] * SECONDS_PER_SOURCE_BYTE for path in files)


def _estimates(durations: dict[str, float], collected: list[str]) -> dict[str, float]:
    values = sorted(durations.values())
    fallback = values[(len(values) * 99 - 1) // 100]
    per_file = {}
    for node, seconds in durations.items():
        path = node.split("::", 1)[0]
        per_file[path] = max(seconds, per_file.get(path, 0.0))
    return {node: durations.get(node, per_file.get(node.split("::", 1)[0], fallback)) for node in collected}


def _combine(argv: list[str]) -> None:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--plans", nargs="+")
    source.add_argument("--from-output", type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--inputs", type=Path)
    args = parser.parse_args(argv)
    if args.from_output:
        outputs = dict(line.split("=", 1) for line in args.from_output.read_text().splitlines())
        fragments = [outputs["queue_312"]]
    else:
        fragments = args.plans
    plan = [leg for fragment in fragments for leg in json.loads("[" + fragment + "]")]
    capped = len(plan) > MAX_SHARDS
    if capped:
        if not (args.from_output and args.root and args.inputs):
            raise SystemExit(f"{len(plan)} queue jobs exceed the pool of {MAX_SHARDS}")
        plan = _capped(plan, args.root, args.inputs)
        args.from_output.write_text(f"queue_312={json.dumps(plan)[1:-1]}\n")
    peak = max(leg["predicted_seconds"] for leg in plan)
    if capped and peak >= STEP_SECONDS:
        print(f"::warning::The capped queue plan predicts {peak:.1f}s, over {STEP_SECONDS}s of the step timeout")
    elif not capped and peak >= STEP_SECONDS / 2:
        raise SystemExit(f"Queue plan exceeds {STEP_SECONDS / 2:.0f}s of the step timeout: {peak:.1f}s")
    print(f"{len(plan)} queue jobs fit one wave of {MAX_SHARDS} runners; predicted slowest test step {peak:.1f}s")


def _capped(plan: list[dict], root: Path, inputs: Path) -> list[dict]:
    needed = {leg["python-version"]: leg["total"] for leg in plan}
    versions = {version: json.loads((inputs / version / ".test_durations").read_text()) for version in needed}
    print(
        f"::warning::{sum(needed.values())} queue jobs exceed the pool of {MAX_SHARDS}; capped at {MAX_SHARDS} shards"
    )
    return matrix(root, versions, dict.fromkeys(needed, MAX_SHARDS))


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "combine":
        _combine(argv[1:])
        return
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--collection", type=Path, required=True)
    parser.add_argument("--reused", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    snapshot = args.root / ".test_durations"
    measured = json.loads(snapshot.read_text())
    collected = json.loads(args.collection.read_text())
    measured = _estimates(measured, collected)
    if args.reused and args.reused.is_file():
        reused = json.loads(args.reused.read_text())
        tests = set(reused.get("tests", ()))
        files = set(reused.get("files", ()))
        measured = {
            node: seconds
            for node, seconds in measured.items()
            if node.split("::", 1)[0] not in files and re.sub(r"@[^\[\]]*$", "", node) not in tests
        }
    plan = matrix(args.root, {args.version: measured})
    snapshot.write_text(json.dumps(measured, indent=4, sort_keys=True) + "\n")
    (args.root / "durations.sha256").write_text(hashlib.sha256(snapshot.read_bytes()).hexdigest() + "\n")
    key = "queue_" + args.version.replace(".", "")
    with args.output.open("a") as output:
        output.write(f"{key}={json.dumps(plan)[1:-1]}\n")


if __name__ == "__main__":
    main()
