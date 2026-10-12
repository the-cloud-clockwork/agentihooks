import argparse
import json
import math
import os
from pathlib import Path

from scripts.ci_mutation.scope import discover_changes, graded_green, own_bases, select_tests
from scripts.ci_mutation.selection import changed_mutations, file_seconds, part_files, stats_shards

# Upper quartile of measured worker seconds per mutant; stored test timings did not predict the cost.
MUTANT_SECONDS = 5.6
WORKERS = 4


def estimate(root: Path, changes: dict[str, set[int]]) -> tuple[float, int, list[str]]:
    mutants, tests = 0, set()
    for path, lines in changes.items():
        chosen = select_tests(root, Path(path))
        if not chosen:
            continue
        tests.update(chosen)
        _, mutations = changed_mutations(path, (root / path).read_text(), lines)
        # mutmut emits only mutants inside a function or method.
        mutants += sum(1 for mutation in mutations if mutation.contained_by_top_level_function)
    return mutants * MUTANT_SECONDS, mutants, sorted(tests)


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
    changes = discover_changes(root, bases, args.head)
    seconds, mutants, tests = estimate(root, changes)
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
            stream.write(f"changed={len(changes)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
