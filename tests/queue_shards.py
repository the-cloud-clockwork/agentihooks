import argparse
import hashlib
import json
import sys
from pathlib import Path

from tests.shards import (
    FIRST_SHARD_FILES,
    SECONDS_PER_SOURCE_BYTE,
    assign_nodes,
    discover_test_files,
    grouped_files,
    source_sizes,
)

WORKERS = 4
STEP_SECONDS = 420
STARTUP_SECONDS = 30
SLOWDOWN = 1.5
MAX_SHARDS = 40


def matrix(root: Path, versions: dict[str, dict[str, float]]) -> list[dict]:
    files = discover_test_files(root)
    grouped = grouped_files(root, files)
    sizes = source_sizes(root, files)
    result = []
    for version, durations in versions.items():
        measured = {node: seconds for node, seconds in durations.items() if node.split("::", 1)[0] in sizes}
        pinned = {node: seconds for node, seconds in measured.items() if node.split("::", 1)[0] in FIRST_SHARD_FILES}
        measured = {node: seconds for node, seconds in measured.items() if node not in pinned}
        count = 12
        while True:
            parts = assign_nodes(measured, count, grouped, WORKERS, sizes=sizes)
            parts[0].extend(pinned)
            peak = max(_seconds(part, {**measured, **pinned}, grouped, sizes) for part in parts)
            predicted = STARTUP_SECONDS + SLOWDOWN * peak
            if predicted < STEP_SECONDS / 2:
                break
            if count == MAX_SHARDS:
                raise SystemExit(f"Python {version} cannot fit below half the step timeout: {predicted:.1f}s")
            count += 1
        print(f"Python {version}: {count} queue shards, predicted slowest {predicted:.1f}s of {STEP_SECONDS / 2:.0f}s")
        result.extend(
            {"python-version": version, "shard": shard, "total": count, "predicted_seconds": predicted}
            for shard in range(1, count + 1)
        )
    return result


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
    source.add_argument("--plans", nargs=2)
    source.add_argument("--from-output", type=Path)
    args = parser.parse_args(argv)
    if args.from_output:
        outputs = dict(line.split("=", 1) for line in args.from_output.read_text().splitlines())
        fragments = [outputs["queue_311"], outputs["queue_312"]]
    else:
        fragments = args.plans
    plan = [leg for fragment in fragments for leg in json.loads("[" + fragment + "]")]
    if len(plan) > MAX_SHARDS:
        raise SystemExit(f"{len(plan)} queue jobs exceed the pool of {MAX_SHARDS}")
    peak = max(leg["predicted_seconds"] for leg in plan)
    if peak >= STEP_SECONDS / 2:
        raise SystemExit(f"Queue plan exceeds half the step timeout: {peak:.1f}s")
    print(f"{len(plan)} queue jobs fit one wave of {MAX_SHARDS} runners; predicted slowest test step {peak:.1f}s")


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "combine":
        _combine(argv[1:])
        return
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--collection", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    snapshot = args.root / ".test_durations"
    measured = json.loads(snapshot.read_text())
    collected = json.loads(args.collection.read_text())
    measured = _estimates(measured, collected)
    plan = matrix(args.root, {args.version: measured})
    snapshot.write_text(json.dumps(measured, indent=4, sort_keys=True) + "\n")
    (args.root / "durations.sha256").write_text(hashlib.sha256(snapshot.read_bytes()).hexdigest() + "\n")
    key = "queue_" + args.version.replace(".", "")
    with args.output.open("a") as output:
        output.write(f"{key}={json.dumps(plan)[1:-1]}\n")


if __name__ == "__main__":
    main()
