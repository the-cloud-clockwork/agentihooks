"""Report the Gate Required commit status on a pull request head from the protected grade and the Tests aggregator."""

import argparse
import json
import subprocess
import time
import urllib.parse
from typing import Any

from scripts import ci_wiring

AGGREGATOR = ci_wiring.GATE
PROTECTED = "Gate — Protected"
ACTIONS_APP = 15368
DESCRIPTIONS = {
    "success": "The protected gate logic and the Tests aggregator passed this head.",
    "failure": "The protected gate logic or the Tests aggregator refused this head; see the run.",
    "pending": "Waiting for the Tests aggregator to grade this head.",
}


def _api(endpoint: str) -> dict[str, Any]:
    return json.loads(
        subprocess.run(["gh", "api", endpoint], check=True, capture_output=True, text=True, timeout=30).stdout
    )


def latest(repository: str, head: str, name: str, suite: int | None = None) -> dict[str, Any] | None:
    """Return the newest GitHub Actions check run called ``name`` on ``head``, limited to ``suite`` when given."""
    query = urllib.parse.urlencode({"check_name": name, "app_id": ACTIONS_APP, "per_page": 100})
    runs = _api(f"repos/{repository}/commits/{head}/check-runs?{query}")["check_runs"]
    named = [run for run in runs if run["name"] == name and suite in (None, run["check_suite"]["id"])]
    return max(named, key=lambda run: run["id"]) if named else None


def aggregator_run(repository: str, head: str) -> dict[str, Any] | None:
    """Return the aggregator check run of the newest pull request Tests run on ``head``, or None before it exists."""
    query = urllib.parse.urlencode({"head_sha": head, "event": "pull_request", "per_page": 1})
    runs = _api(f"repos/{repository}/actions/workflows/test.yml/runs?{query}")["workflow_runs"]
    return latest(repository, head, AGGREGATOR, runs[0]["check_suite_id"]) if runs else None


def protected_result(repository: str, head: str, polls: int = 20, interval: float = 10) -> str:
    """Return the protected grade's result, waiting up to ``polls`` reads while its check run is still running."""
    for _ in range(polls - 1):
        result = _result(latest(repository, head, PROTECTED))
        if result != "pending":
            return result
        time.sleep(interval)
    return _result(latest(repository, head, PROTECTED))


def _result(run: dict[str, Any] | None) -> str:
    if run is None or run["status"] != "completed":
        return "pending"
    return "success" if run["conclusion"] == "success" else "failure"


def decide(protected: str, aggregator: str) -> str:
    """Return the Gate Required state: success only once both the protected grade and the aggregator succeeded."""
    if "failure" in (protected, aggregator):
        return "failure"
    if protected == aggregator == "success":
        return "success"
    return "pending"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--protected", choices=("success", "failure"))
    parser.add_argument("--target-url", default="")
    args = parser.parse_args(argv)
    protected = args.protected or protected_result(args.repository, args.head)
    state = decide(protected, _result(aggregator_run(args.repository, args.head)))
    subprocess.run(
        [
            "gh",
            "api",
            f"repos/{args.repository}/statuses/{args.head}",
            "-f",
            f"state={state}",
            "-f",
            f"context={AGGREGATOR}",
            "-f",
            f"target_url={args.target_url}",
            "-f",
            f"description={DESCRIPTIONS[state]}",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    print(f"Gate Required on {args.head[:12]}: {state} (protected {protected}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
