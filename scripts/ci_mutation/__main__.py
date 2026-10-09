import argparse
import os
from pathlib import Path

from scripts.ci_mutation.runner import run_gate
from scripts.ci_mutation.scope import discover_changes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="origin/dev")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--output", type=Path, default=Path(".mutation-gate"))
    parser.add_argument("--budget", type=float, default=1080)
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=1)
    args = parser.parse_args()
    if not 0 <= args.shard < args.shards:
        parser.error(f"--shard {args.shard} is outside 0 to {args.shards - 1}")
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    root = Path.cwd()
    changes = discover_changes(root, args.base, args.head)
    print(f"Changed Python files: {len(changes)}", flush=True)
    print(f"Mutation shard: {args.shard + 1} of {args.shards}")
    report = run_gate(root, changes, args.output.resolve(), args.budget, (args.shard, args.shards))
    return int(report["failed"])


if __name__ == "__main__":
    raise SystemExit(main())
