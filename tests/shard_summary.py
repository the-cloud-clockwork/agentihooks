import argparse
import json
from pathlib import Path

from tests.shard_check import run_counts


def _branch(plan: Path | None, collected: Path) -> tuple[set[str], str]:
    if plan is None or not plan.is_file():
        return set(), ""
    recorded = json.loads(plan.read_text())
    runs = " ".join(str(entry["run"]) for entry in recorded["runs"])
    tests = set(recorded["tests"]).intersection(json.loads(collected.read_text()))
    return tests, f"pull request runs {runs}" if runs else "no pull request run"


def summary(
    version: str, durations: list[Path], collected: Path, plan: Path, flakes: list[Path], branch: Path | None = None
) -> list[str]:
    counts = run_counts(durations)
    flaky = sum(
        json.loads(line).get("outcome") == "flaky"
        for path in flakes
        if path.is_file()
        for line in path.read_text().splitlines()
        if line.strip()
    )
    planned: set[str] = set()
    source = "no plan"
    if plan.is_file():
        recorded = json.loads(plan.read_text())
        planned = set(recorded["tests"]).intersection(json.loads(collected.read_text()))
        source = f"dev run {recorded['run']}"
    branched, runs = _branch(branch, collected)
    branched -= planned
    reused = f"reused {len(planned - counts.keys())} from {source}"
    if runs:
        reused += f", reused {len(branched - counts.keys())} from {runs}"
    lines = [f"Python {version}: ran {len(counts)}, {reused}, flaky {flaky}"]
    for path in durations:
        ran = run_counts([path])
        if (planned | branched) & ran.keys():
            lines.append(f"{path.parent.name} fell back: ran {len(ran)}, reused 0")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="summarize the tests the unit shards reused, ran and found flaky")
    parser.add_argument("--version", required=True)
    parser.add_argument("--durations", type=Path, nargs="+", required=True, help="the durations.json each shard stored")
    parser.add_argument("--collected", type=Path, required=True)
    parser.add_argument(
        "--plan", type=Path, required=True, help="the test plan whose tests reuse their passed dev result"
    )
    parser.add_argument("--flakes", type=Path, nargs="*", default=[])
    parser.add_argument("--branch-plan", type=Path, help="the plan whose tests reuse a pull request run's pass")
    args = parser.parse_args(argv)
    for line in summary(args.version, args.durations, args.collected, args.plan, args.flakes, args.branch_plan):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
