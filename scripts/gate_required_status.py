"""Report the Gate Required commit status on a pull request head from the protected grade and the Tests aggregator."""

import argparse
import json
import subprocess
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


def latest(repository: str, head: str, name: str) -> dict[str, Any] | None:
    """Return the newest GitHub Actions check run called ``name`` on ``head``, or None when it does not exist yet."""
    query = urllib.parse.urlencode({"check_name": name, "app_id": ACTIONS_APP, "per_page": 100})
    runs = _api(f"repos/{repository}/commits/{head}/check-runs?{query}")["check_runs"]
    named = [run for run in runs if run["name"] == name]
    return max(named, key=lambda run: run["id"]) if named else None


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
    protected = args.protected or _result(latest(args.repository, args.head, PROTECTED))
    state = decide(protected, _result(latest(args.repository, args.head, AGGREGATOR)))
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
