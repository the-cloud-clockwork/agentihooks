import argparse
import json
import math
import os
from pathlib import Path

from scripts.ci_mutation.scope import discover_changes, select_tests
from scripts.ci_mutation.selection import changed_mutations

# Fitted on pull request runs: each mutant costs a pytest start plus a few of its covering tests.
MUTANT_SECONDS = 3.0
TEST_WEIGHT = 5.0
UNTIMED_TEST_SECONDS = 1.0
WORKERS = 4


def mean_test_seconds(durations: dict[str, float], tests: list[str]) -> float:
    selected = set(tests)
    timings = [seconds for nodeid, seconds in durations.items() if nodeid.partition("::")[0] in selected]
    return sum(timings) / len(timings) if timings else UNTIMED_TEST_SECONDS


def estimate(root: Path, changes: dict[str, set[int]]) -> float:
    stored = root / ".test_durations"
    durations = json.loads(stored.read_text()) if stored.is_file() else {}
    seconds = 0.0
    for path, lines in changes.items():
        tests = select_tests(root, Path(path))
        if not tests:
            continue
        _, mutations = changed_mutations(path, (root / path).read_text(), lines)
        seconds += len(mutations) * (MUTANT_SECONDS + TEST_WEIGHT * mean_test_seconds(durations, tests))
    return seconds


def shard_count(seconds: float, target: float, limit: int) -> int:
    return max(1, min(limit, math.ceil(seconds / (WORKERS * target))))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="origin/dev")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--target", type=float, default=240)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    root = Path.cwd()
    seconds = estimate(root, discover_changes(root, args.base, args.head))
    count = shard_count(seconds, args.target, args.limit)
    print(f"Estimated mutation seconds: {seconds:.0f}\nMutation shards: {count}")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a") as stream:
            stream.write(f"shards={json.dumps(list(range(count)))}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
