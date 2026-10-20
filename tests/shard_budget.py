import argparse
import json
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/test.yml"
STEPS = {"unit": "Run tests", "unit-selected": "Run the selected tests"}


def step_seconds(job: str) -> int:
    steps = yaml.safe_load(WORKFLOW.read_text())["jobs"][job]["steps"]
    return next(step for step in steps if step.get("name") == STEPS[job])["timeout-minutes"] * 60


def _seconds(durations: Path) -> tuple[float, str]:
    elapsed = durations.parent / "elapsed.json"
    if elapsed.is_file():
        record = json.loads(elapsed.read_text())
        return float(record["seconds"]), record.get("job", "unit")
    return sum(json.loads(durations.read_text()).values()), "unit"


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
    measured = {path: _seconds(path) for path in args.durations}
    seconds = {path: value for path, (value, _) in measured.items()}
    ranked = sorted(seconds, key=lambda shard: -seconds[shard])
    limits = {job: step_seconds(job) if args.budget is None else args.budget for _, job in measured.values()}
    budgets = {path: limits[job] for path, (_, job) in measured.items()}
    job = measured[ranked[0]][1]
    limit = "a budget of"
    if args.budget is None:
        limit = f"the {limits[job] // 60} minute {STEPS[job]} deadline of"
    print(f"{len(seconds)} shards, slowest {seconds[ranked[0]]:.1f} s of test time against {limit} {limits[job]:g} s")
    over = [shard for shard in ranked if seconds[shard] > budgets[shard]]
    for shard in ranked:
        print(f"{shard.parent.name} {seconds[shard]:.1f} s{' over budget' if shard in over else ''}")
    if over:
        level = "warning" if args.report else "error"
        budget = " or ".join(sorted({f"{budgets[shard]:g}" for shard in over}))
        print(f"::{level}::{len(over)} of {len(seconds)} shards passed the {budget} s budget.")
        return 0 if args.report else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
