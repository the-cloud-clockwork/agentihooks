import argparse
import json
from pathlib import Path

RUN_TESTS_MINUTES = 12


def _seconds(durations: Path) -> float:
    elapsed = durations.parent / "elapsed.json"
    if elapsed.is_file():
        return float(json.loads(elapsed.read_text())["seconds"])
    return sum(json.loads(durations.read_text()).values())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="fail when a shard's elapsed test step time, else its summed test time, passes the budget"
    )
    parser.add_argument("durations", type=Path, nargs="+", help="the durations.json each shard stored")
    parser.add_argument("--budget", type=float, help="seconds of test time per shard")
    parser.add_argument("--report", action="store_true", help="warn on a shard over budget instead of failing")
    args = parser.parse_args(argv)
    unread = [str(path) for path in args.durations if not path.is_file()]
    if unread:
        print(f"::error::No shard durations at {', '.join(unread)}, so those shards cannot be graded.")
        return 1
    seconds = {path: _seconds(path) for path in args.durations}
    ranked = sorted(seconds, key=lambda shard: -seconds[shard])
    limit = "a budget of"
    if args.budget is None:
        args.budget = RUN_TESTS_MINUTES * 60
        limit = f"the {RUN_TESTS_MINUTES} minute Run tests deadline of"
    budget = f"{args.budget:g}"
    print(f"{len(seconds)} shards, slowest {seconds[ranked[0]]:.1f} s of test time against {limit} {budget} s")
    over = [shard for shard in ranked if seconds[shard] > args.budget]
    for shard in ranked:
        print(f"{shard.parent.name} {seconds[shard]:.1f} s{' over budget' if shard in over else ''}")
    if over:
        level = "warning" if args.report else "error"
        print(f"::{level}::{len(over)} of {len(seconds)} shards passed the {budget} s budget.")
        return 0 if args.report else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
