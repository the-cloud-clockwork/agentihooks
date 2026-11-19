"""Cancels the runs GitHub left queued with no update for a day, after logging the states of their jobs."""

import argparse
import json
import subprocess
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from scripts.ci_budget import rate_limit

AGE = timedelta(hours=24)
SETTLE_SECONDS = 30
STOPS = ("cancel", "force-cancel")

gh_api = cast("Callable[..., subprocess.CompletedProcess[str]]", rate_limit.gh_api)


def _api(*args: str) -> str:
    return gh_api(list(args), timeout=120, text=True).stdout


def queued(repo: str) -> list[dict[str, Any]]:
    url = f"repos/{repo}/actions/runs?status=queued&per_page=100"
    return [json.loads(line) for line in _api("--paginate", url, "--jq", ".workflow_runs[]").splitlines()]


def stale(runs: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Keep the runs with no update for ``AGE``; GitHub cancels a job queued that long, so such a run starts none."""
    return [run for run in runs if now - datetime.fromisoformat(run["updated_at"]) >= AGE]


def jobs(repo: str, run_id: int) -> str:
    url = f"repos/{repo}/actions/runs/{run_id}/jobs?filter=all&per_page=100"
    found = [json.loads(line) for line in _api("--paginate", url, "--jq", ".jobs[]").splitlines()]
    states = Counter(f"{job['status']} on {','.join(job['labels'])}" for job in found)
    return ", ".join(f"{count} {state}" for state, count in sorted(states.items())) or "no jobs"


def clear(repo: str, runs: list[dict[str, Any]]) -> list[int]:
    """Post cancel to every run, then force-cancel to the runs still queued, and return the ids that stay queued."""
    for run in runs:
        when = f"created {run['created_at']}, updated {run['updated_at']}"
        print(f"run {run['id']} {run['name']} on {run['head_branch']}, {when}: {jobs(repo, run['id'])}")
    left = {run["id"] for run in runs}
    for action in STOPS:
        for run_id in sorted(left):
            try:
                _api("-X", "POST", f"repos/{repo}/actions/runs/{run_id}/{action}")
            except subprocess.CalledProcessError as error:
                print(f"run {run_id} refused {action}: {str(error.stderr).strip()}")
        time.sleep(SETTLE_SECONDS)
        left &= {run["id"] for run in queued(repo)}
        print(f"{len(left)} of {len(runs)} stale runs still queued after {action}")
        if not left:
            break
    return sorted(left)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    args = parser.parse_args(argv)
    runs = stale(queued(args.repo), datetime.now(UTC))
    print(f"{len(runs)} stale queued runs")
    if not runs:
        return 0
    for run_id in clear(args.repo, runs):
        print(f"::warning::run {run_id} stays queued after cancel and force-cancel")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
