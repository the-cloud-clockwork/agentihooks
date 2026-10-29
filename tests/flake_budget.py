"""Keeps seven days of dev flake reports and names every test flaky in two separate runs of them."""

import argparse
import json
import re
import sys
import time
from pathlib import Path

WINDOW_S = 7 * 24 * 3600
BUDGET_RUNS = 2
PARAMETERS = re.compile(r"\[.*\]$")


def gather(history: list[dict], reports: list[dict], now_s: float) -> list[dict]:
    kept = [line for line in history if line["at"] > now_s - WINDOW_S]
    seen = {(line["test"], line["run"]): line for line in kept}
    for report in reports:
        if report["outcome"] != "flaky":
            continue
        line = {"test": PARAMETERS.sub("", report["test"]), "run": str(report["run"]), "at": now_s}
        key = (line["test"], line["run"])
        if key not in seen:
            seen[key] = line
            kept.append(line)
        if report.get("tracebacks"):
            evidence = seen[key].setdefault("reports", [])
            if report not in evidence:
                evidence.append(report)
    return kept


def budget(history: list[dict]) -> dict:
    runs = {}
    for line in history:
        runs.setdefault(line["test"], set()).add(line["run"])
    retried = [{"test": test, "count": len(ids), "runs": sorted(ids)} for test, ids in sorted(runs.items())]
    for test in retried:
        reports = [report for line in history if line["test"] == test["test"] for report in line.get("reports", [])]
        if reports:
            test["reports"] = reports
    over = [{"test": test["test"], "runs": test["runs"]} for test in retried if test["count"] >= BUDGET_RUNS]
    return {"flaky_tests": len(runs), "over_budget": over, "retried": retried}


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--reports", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--now", type=float, default=None)
    args = parser.parse_args(argv)
    files = sorted(args.reports.rglob("*.jsonl"))
    if not files:
        print(f"::error::No flake reports under {args.reports}")
        return 1
    history = _lines(args.history) if args.history.exists() else []
    now_s = time.time() if args.now is None else args.now
    kept = gather(history, [report for path in files for report in _lines(path)], now_s)
    args.history.parent.mkdir(parents=True, exist_ok=True)
    args.history.write_text("".join(json.dumps(line) + "\n" for line in kept))
    found = budget(kept)
    args.out.write_text(json.dumps(found, indent=2) + "\n")
    print(f"Flaky test ids in the last seven days of dev runs: {found['flaky_tests']}")
    for test in found["retried"]:
        print(f"{test['test']} passed on a retry in {test['count']} dev runs: {', '.join(test['runs'])}")
    for test in found["over_budget"]:
        print(
            f"::warning title=Flake budget::{test['test']} was flaky in {len(test['runs'])} dev runs: {', '.join(test['runs'])}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
