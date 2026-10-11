import argparse
import json
import math
import os
from pathlib import Path

from scripts.ci_mutation.scope import discover_changes, graded_green, own_bases, select_tests
from scripts.ci_mutation.selection import changed_mutations, file_seconds, part_files, stats_shards

# Fitted on Tests runs 37890680008 and 37877992114: a mutant costs a pytest start plus a few covering tests.
MUTANT_SECONDS = 3.0
TEST_WEIGHT = 5.0
UNTIMED_TEST_SECONDS = 1.0
WORKERS = 4


def mean_test_seconds(durations: dict[str, float], tests: list[str]) -> float:
    selected = set(tests)
    timings = [seconds for nodeid, seconds in durations.items() if nodeid.partition("::")[0] in selected]
    return sum(timings) / len(timings) if timings else UNTIMED_TEST_SECONDS


def estimate(root: Path, changes: dict[str, set[int]]) -> tuple[float, int, list[str]]:
    stored = root / ".test_durations"
    durations = json.loads(stored.read_text()) if stored.is_file() else {}
    seconds, mutants, tests = 0.0, 0, set()
    for path, lines in changes.items():
        chosen = select_tests(root, Path(path))
        if not chosen:
            continue
        tests.update(chosen)
        _, mutations = changed_mutations(path, (root / path).read_text(), lines)
        # mutmut emits only mutants inside a function or method.
        count = sum(1 for mutation in mutations if mutation.contained_by_top_level_function)
        mutants += count
        seconds += count * (MUTANT_SECONDS + TEST_WEIGHT * mean_test_seconds(durations, chosen))
    return seconds, mutants, sorted(tests)


def shard_count(seconds: float, mutants: int, target: float, limit: int) -> int:
    return max(1, min(limit, mutants, math.ceil(seconds / (WORKERS * target))))


def slowest_bucket(root: Path, tests: list[str], parts: int) -> float:
    seconds = file_seconds(root, tests)
    buckets = [
        bucket
        for index in range(parts)
        for bucket in stats_shards(root, part_files(root, tests, (index, parts)), WORKERS)
    ]
    return max(sum(seconds[path] for path in bucket) for bucket in buckets)


def stats_part_count(root: Path, tests: list[str], target: float, limit: int) -> tuple[int, float]:
    walls = [slowest_bucket(root, tests, parts) for parts in range(1, limit + 1)]
    # One file never splits, so past the floor another part only adds a job.
    reachable = max(target, min(walls))
    parts = next(parts for parts, wall in enumerate(walls, 1) if wall <= reachable)
    return parts, walls[parts - 1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="origin/dev")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--target", type=float, default=240)
    parser.add_argument("--stats-target", type=float, default=30)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    root = Path.cwd()
    bases = own_bases(root, args.base, args.head, graded_green)
    print(f"Mutation bases: {' '.join(bases)}")
    seconds, mutants, tests = estimate(root, discover_changes(root, bases, args.head))
    count = shard_count(seconds, mutants, args.target, args.limit)
    parts, stats = stats_part_count(root, tests, args.stats_target, args.limit)
    print(
        f"Changed line mutants: {mutants}\nEstimated mutation seconds: {seconds:.0f}, slowest stats bucket: {stats:.0f}"
    )
    print(f"Mutation shards: {count}\nStats parts: {parts}")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a") as stream:
            stream.write(f"shards={json.dumps(list(range(count)))}\n")
            stream.write(f"stats_parts={json.dumps(list(range(parts)))}\n")
            stream.write(f"bases={','.join(bases)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
