import argparse
import json
from pathlib import Path

FIFTEEN_MINUTES = 900


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="fail when a shard's summed test time passes the budget")
    parser.add_argument("durations", type=Path, nargs="+", help="the durations.json each shard stored")
    parser.add_argument("--budget", type=float, default=FIFTEEN_MINUTES, help="seconds of test time per shard")
    args = parser.parse_args(argv)
    unread = [str(path) for path in args.durations if not path.is_file()]
    if unread:
        print(f"::error::No shard durations at {', '.join(unread)}, so those shards cannot be graded.")
        return 1
    seconds = {path.parent.name: sum(json.loads(path.read_text()).values()) for path in args.durations}
    ranked = sorted(seconds, key=lambda shard: -seconds[shard])
    budget = f"{args.budget:g}"
    print(f"{len(seconds)} shards, slowest {seconds[ranked[0]]:.1f} s against a budget of {budget} s")
    over = [shard for shard in ranked if seconds[shard] > args.budget]
    for shard in ranked:
        print(f"{shard} {seconds[shard]:.1f} s{' over budget' if shard in over else ''}")
    if over:
        print(f"::error::{len(over)} of {len(seconds)} shards passed the {budget} s budget.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
