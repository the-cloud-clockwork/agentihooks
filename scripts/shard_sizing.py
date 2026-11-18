"""Shard sizing the CI lab, the CI selector and the test shards share: measured costs and the greedy work split."""

from pathlib import Path

SLOWDOWN = 1.69
SECONDS_PER_SOURCE_BYTE = 3e-6
FIRST_SHARD_FILES = frozenset({"tests/routing/test_codex_api.py"})


def source_sizes(root: Path, files: list[str]) -> dict[str, int]:
    return {path: (root / path).stat().st_size for path in files}


def assign_work(
    seconds: dict[str, float],
    serial: dict[str, float],
    collection: dict[str, float],
    shards: int,
    workers: int,
    serial_groups: dict[str, str] | None = None,
    collection_groups: dict[str, str] | None = None,
) -> list[list[str]]:
    loads = [0.0] * shards
    serial_loads: list[dict[str, float]] = [{} for _ in range(shards)]
    serial_peaks = [0.0] * shards
    collection_loads = [0.0] * shards
    collected: list[set[str]] = [set() for _ in range(shards)]
    groups: list[list[str]] = [[] for _ in range(shards)]
    for path in sorted(seconds, key=lambda f: (-max(seconds[f] / workers, serial[f]) - collection[f], f)):
        group = serial_groups[path] if serial_groups is not None else ""
        file = collection_groups[path] if collection_groups is not None else path
        lightest = min(
            range(shards),
            key=lambda i: (
                max(
                    (loads[i] + seconds[path]) / workers,
                    serial_peaks[i],
                    serial_loads[i].get(group, 0.0) + serial[path],
                )
                + collection_loads[i]
                + (collection[path] if file not in collected[i] else 0.0),
                loads[i],
            ),
        )
        loads[lightest] += seconds[path]
        total = serial_loads[lightest].get(group, 0.0) + serial[path]
        serial_loads[lightest][group] = total
        serial_peaks[lightest] = max(serial_peaks[lightest], total)
        if file not in collected[lightest]:
            collection_loads[lightest] += collection[path]
            collected[lightest].add(file)
        groups[lightest].append(path)
    return groups
