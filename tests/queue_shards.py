import argparse
import json
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
STARTUP_SECONDS = 60
SLOWDOWN = 1.5
MAX_SHARDS = 128


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
            count = min(count * 2, MAX_SHARDS)
        print(f"Python {version}: {count} queue shards, predicted slowest {predicted:.1f}s of {STEP_SECONDS / 2:.0f}s")
        result.extend({"python-version": version, "shard": shard, "total": count} for shard in range(1, count + 1))
    return result


def _seconds(part: list[str], durations: dict[str, float], grouped: dict[str, str], sizes: dict[str, int]) -> float:
    serial = {}
    files = {node.split("::", 1)[0] for node in part}
    for node in part:
        if group := grouped.get(node.split("::", 1)[0]):
            serial[group] = serial.get(group, 0.0) + durations[node]
    return max(sum(durations[node] for node in part) / WORKERS, max(serial.values(), default=0.0)) + sum(
        sizes[path] * SECONDS_PER_SOURCE_BYTE for path in files
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--durations", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    versions = {}
    for version in ("3.11", "3.12"):
        merged = json.loads((args.durations / ".test_durations").read_text())
        path = args.durations / f".test_durations-{version}"
        versions[version] = {**merged, **(json.loads(path.read_text()) if path.exists() else {})}
    plan = matrix(args.root, versions)
    with args.output.open("a") as output:
        output.write(f"queue_matrix={json.dumps(plan)}\n")


if __name__ == "__main__":
    main()
