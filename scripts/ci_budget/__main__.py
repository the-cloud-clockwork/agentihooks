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
    if ci_budget.run_state(result["total"]) == "over":
        lines.append(
            f"::error title=Run over fifteen minutes::push to this report took {ci_budget.clock(result['total'])}"
            f" against {ci_budget.clock(ci_budget.RUN_BUDGET_S)}"
        )
    return lines


def _summary(result: dict) -> str:
    rows = [
        f"| {row['stage']} | {ci_budget.clock(row['seconds'])} | {ci_budget.budget_cell(row)} | {ci_budget.verdict(row)} |"
        for row in result["stages"]
    ]
    total = result["total"]
    return "\n".join(
        [
            "## Stage budget",
            "",
            "| Stage | Wall | Budget | Verdict |",
            "| --- | ---: | ---: | --- |",
            *rows,
            f"| push to this report | {ci_budget.clock(total)} | {ci_budget.clock(ci_budget.RUN_BUDGET_S)} | "
            f"{ci_budget.run_state(total)} |",
            "",
        ]
    )


def main(argv: list[str] | None = None, now: Callable[[], float] = time.time) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--jobs", type=Path, required=True)
    args = parser.parse_args(argv)
    run_doc = json.loads(args.run.read_text())
    jobs = [json.loads(line) for line in args.jobs.read_text().splitlines() if line]
    if not jobs:
        print(f"::error::{args.jobs} lists no jobs, so no stage can be measured.")
        return 1
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
