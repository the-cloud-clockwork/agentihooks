import argparse
import json
from pathlib import Path

from tests.shard_check import run_counts


def summary(version: str, durations: list[Path], collected: Path, plan: Path, flakes: list[Path]) -> list[str]:
    counts = run_counts(durations)
    flaky = sum(
        json.loads(line).get("outcome") == "flaky"
        for path in flakes
        if path.is_file()
        for line in path.read_text().splitlines()
        if line.strip()
    )
    reused: set[str] = set()
    source = "no plan"
    if plan.is_file():
        recorded = json.loads(plan.read_text())
        reused = set(recorded["tests"]).intersection(json.loads(collected.read_text()))
        source = f"dev run {recorded['run']}"
    return [f"Python {version}: ran {len(counts)}, reused {len(reused)} from {source}, flaky {flaky}"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="summarize the tests the unit shards reused, ran and found flaky")
    parser.add_argument("--version", required=True)
    parser.add_argument("--durations", type=Path, nargs="+", required=True, help="the durations.json each shard stored")
    parser.add_argument("--collected", type=Path, required=True)
    parser.add_argument(
        "--plan", type=Path, required=True, help="the test plan whose tests reuse their passed dev result"
    )
    parser.add_argument("--flakes", type=Path, nargs="*", default=[])
    args = parser.parse_args(argv)
    for line in summary(args.version, args.durations, args.collected, args.plan, args.flakes):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
