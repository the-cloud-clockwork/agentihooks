import argparse
import os
import subprocess
from pathlib import Path

from scripts.ci_mutation.runner import run_gate
from scripts.ci_mutation.scope import blob, discover_changes, is_test_file
from scripts.ci_mutation.stats import SharedStats
from tests.dep_manifest import changed_units


def branch_tests(root: Path, base: str, head: str) -> tuple[str, ...]:
    """Name every test function the branch adds or changes since it left base, as pytest node ids."""
    fork = subprocess.check_output(["git", "merge-base", base, head], cwd=root).decode().strip()
    command = ["git", "diff", "--name-only", "--no-renames", "-z", fork, head, "--", "tests"]
    found = set()
    for name in subprocess.check_output(command, cwd=root).decode().split("\0"):
        new = blob(root, head, name) if is_test_file(name) else None
        if new is None:
            continue
        old = blob(root, fork, name)
        for unit in changed_units(old.decode() if old is not None else None, new.decode()):
            if unit.rpartition(".")[2].startswith("test"):
                found.add(f"{name}::{unit.replace('.', '::')}")
    return tuple(sorted(found))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="origin/dev")
    parser.add_argument("--bases")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--output", type=Path, default=Path(".mutation-gate"))
    parser.add_argument("--budget", type=float, default=1080)
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--stats", type=Path)
    parser.add_argument("--stats-part", type=int)
    parser.add_argument("--stats-parts", type=int, default=1)
    args = parser.parse_args()
    if not 0 <= args.shard < args.shards:
        parser.error(f"--shard {args.shard} is outside 0 to {args.shards - 1}")
    if args.stats_part is not None and not (args.stats and 0 <= args.stats_part < args.stats_parts):
        parser.error(f"--stats-part {args.stats_part} needs --stats and is outside 0 to {args.stats_parts - 1}")
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    root = Path.cwd()
    if os.environ.get("MUTATION_STATS_CACHE"):
        os.environ["MUTATION_STATS_ROOT"] = str(root)
    bases = args.bases.split(",") if args.bases is not None else args.base
    changes = discover_changes(root, bases, args.head)
    print(f"Changed Python files: {len(changes)}", flush=True)
    first = branch_tests(root, bases[0] if isinstance(bases, list) else bases, args.head) if changes else ()
    print(f"Tests new or changed on the branch, run first: {len(first)}", flush=True)
    stats = None
    if args.stats:
        head = subprocess.run(["git", "rev-parse", args.head], cwd=root, capture_output=True, text=True, check=True)
        part = None if args.stats_part is None else (args.stats_part, args.stats_parts)
        stats = SharedStats(args.stats.resolve(), head.stdout.strip(), part)
        print(f"Mutation stats part: {part[0] + 1} of {part[1]}" if part else f"Mutation stats from {args.stats}")
    print(f"Mutation shard: {args.shard + 1} of {args.shards}")
    report = run_gate(root, changes, args.output.resolve(), args.budget, (args.shard, args.shards), stats, first)
    if (failing := report.get("failing_tests")) and (summary := os.environ.get("GITHUB_STEP_SUMMARY")):
        with Path(summary).open("a") as stream:
            stream.write("### Mutation clean baseline failed\n\n" + "".join(f"- `{test}`\n" for test in failing))
    return int(report["failed"])


if __name__ == "__main__":
    raise SystemExit(main())
