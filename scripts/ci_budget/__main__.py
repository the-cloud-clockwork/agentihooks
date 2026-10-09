import argparse
import json
import os
import time
from collections.abc import Callable
from pathlib import Path

from scripts import ci_budget


def _annotations(result: dict) -> list[str]:
    lines = []
    for row in result["stages"]:
        if row["budget"] is None:
            lines.append(
                f"::warning title=Stage without a budget::{row['stage']} took {ci_budget.clock(row['seconds'])}"
                " and has no budget"
            )
        elif ci_budget.over(row):
            lines.append(
                f"::warning title=Stage over budget::{row['stage']} took {ci_budget.clock(row['seconds'])}"
                f" against its budget of {ci_budget.clock(row['budget'])}"
            )
    if result["total"] > ci_budget.RUN_BUDGET_S:
        lines.append(
            f"::error title=Run over fifteen minutes::push to this report took {ci_budget.clock(result['total'])}"
            f" against {ci_budget.clock(ci_budget.RUN_BUDGET_S)}"
        )
    return lines


def _summary(result: dict) -> str:
    rows = [
        f"| {row['stage']} | {ci_budget.clock(row['seconds'])} | "
        f"{'none' if row['budget'] is None else ci_budget.clock(row['budget'])} | {ci_budget.verdict(row)} |"
        for row in result["stages"]
    ]
    total = result["total"]
    state = "over" if total > ci_budget.RUN_BUDGET_S else "ok"
    return "\n".join(
        [
            "## Stage budget",
            "",
            "| Stage | Wall | Budget | Verdict |",
            "| --- | ---: | ---: | --- |",
            *rows,
            f"| push to this report | {ci_budget.clock(total)} | {ci_budget.clock(ci_budget.RUN_BUDGET_S)} | {state} |",
            "",
        ]
    )


def main(argv: list[str] | None = None, now: Callable[[], float] = time.time) -> int:
    parser = argparse.ArgumentParser(description="print each Tests stage's wall time against its budget")
    parser.add_argument("--run", type=Path, required=True, help="the run as the runs endpoint returns it")
    parser.add_argument("--jobs", type=Path, required=True, help="this attempt's jobs, one JSON object per line")
    args = parser.parse_args(argv)
    run_doc = json.loads(args.run.read_text())
    jobs = [json.loads(line) for line in args.jobs.read_text().splitlines() if line]
    result = ci_budget.report(run_doc, jobs, now())
    print("\n".join(ci_budget.render(result)))
    print("\n".join(_annotations(result)))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a") as handle:
            handle.write(_summary(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
