import argparse
import json
import math
import os
from pathlib import Path

from scripts.ci_mutation.scope import discover_changes, select_tests
from scripts.ci_mutation.selection import changed_mutations

# Fitted on Tests runs 37890680008 and 37877992114: a mutant costs a pytest start plus a few covering tests.
MUTANT_SECONDS = 3.0
TEST_WEIGHT = 5.0
UNTIMED_TEST_SECONDS = 1.0
WORKERS = 4


def mean_test_seconds(durations: dict[str, float], tests: list[str]) -> float:
    selected = set(tests)
    timings = [seconds for nodeid, seconds in durations.items() if nodeid.partition("::")[0] in selected]
    return sum(timings) / len(timings) if timings else UNTIMED_TEST_SECONDS


def estimate(root: Path, changes: dict[str, set[int]]) -> tuple[float, int, float]:
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
    stats = sum(duration for nodeid, duration in durations.items() if nodeid.partition("::")[0] in tests)
    return seconds, mutants, stats


def shard_count(seconds: float, mutants: int, stats: float, target: float, limit: int) -> int:
    # Every shard repeats the stats run over all selected tests before mutating its share.
    capacity = max(WORKERS * target - stats, WORKERS * target / 2)
    return max(1, min(limit, mutants, math.ceil(seconds / capacity)))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="origin/dev")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--target", type=float, default=240)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    root = Path.cwd()
    seconds, mutants, stats = estimate(root, discover_changes(root, args.base, args.head))
    count = shard_count(seconds, mutants, stats, args.target, args.limit)
    print(f"Changed line mutants: {mutants}\nEstimated mutation seconds: {seconds:.0f}, stats seconds: {stats:.0f}")
    print(f"Mutation shards: {count}")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a") as stream:
            stream.write(f"shards={json.dumps(list(range(count)))}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
