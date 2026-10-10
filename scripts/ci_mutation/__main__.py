import argparse
import os
import subprocess
from pathlib import Path

from scripts.ci_mutation.runner import run_gate
from scripts.ci_mutation.scope import discover_changes
from scripts.ci_mutation.stats import SharedStats


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
    changes = discover_changes(root, args.bases.split(",") if args.bases else args.base, args.head)
    print(f"Changed Python files: {len(changes)}", flush=True)
    stats = None
    if args.stats:
        head = subprocess.run(["git", "rev-parse", args.head], cwd=root, capture_output=True, text=True, check=True)
        part = None if args.stats_part is None else (args.stats_part, args.stats_parts)
        stats = SharedStats(args.stats.resolve(), head.stdout.strip(), part)
        print(f"Mutation stats part: {part[0] + 1} of {part[1]}" if part else f"Mutation stats from {args.stats}")
    print(f"Mutation shard: {args.shard + 1} of {args.shards}")
    report = run_gate(root, changes, args.output.resolve(), args.budget, (args.shard, args.shards), stats)
    return int(report["failed"])


if __name__ == "__main__":
    raise SystemExit(main())
