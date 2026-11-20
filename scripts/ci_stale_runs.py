"""Clears the runs GitHub left queued with no update for a day, after logging the states of their jobs."""

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

gh_api = cast("Callable[..., subprocess.CompletedProcess[str]]", rate_limit.gh_api)


def _api(*args: str) -> str:
    return gh_api(list(args), timeout=120, text=True).stdout


def queued(repo: str) -> list[dict[str, Any]]:
    url = f"repos/{repo}/actions/runs?status=queued&per_page=100"
    return [json.loads(line) for line in _api("--paginate", url, "--jq", ".workflow_runs[]").splitlines()]


def stale(runs: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Keep the runs with no update for ``AGE``; GitHub cancels a job queued that long, so such a run starts none."""
    return [run for run in runs if now - datetime.fromisoformat(run["updated_at"]) >= AGE]


def jobs(repo: str, run_id: int) -> list[dict[str, Any]]:
    url = f"repos/{repo}/actions/runs/{run_id}/jobs?filter=all&per_page=100"
    return [json.loads(line) for line in _api("--paginate", url, "--jq", ".jobs[]").splitlines()]


def states(found: list[dict[str, Any]]) -> str:
    counts = Counter(f"{job['status']} on {','.join(job['labels'])}" for job in found)
    return ", ".join(f"{count} {state}" for state, count in sorted(counts.items())) or "no jobs"


def clear(repo: str, runs: list[dict[str, Any]]) -> list[int]:
    """Delete each run that holds no job, cancel the others, and return the ids that stay queued.

    GitHub refuses cancel and force-cancel on a queued run with no job, answering 409 that it has not been queued yet.
    """
    for run in runs:
        found = jobs(repo, run["id"])
        when = f"created {run['created_at']}, updated {run['updated_at']}"
        print(f"run {run['id']} {run['name']} on {run['head_branch']}, {when}: {states(found)}")
        path = f"repos/{repo}/actions/runs/{run['id']}"
        action, request = ("cancel", ("-X", "POST", f"{path}/cancel")) if found else ("delete", ("-X", "DELETE", path))
        try:
            _api(*request)
        except subprocess.CalledProcessError as error:
            print(f"run {run['id']} refused {action}: {str(error.stderr).strip()}")
    time.sleep(SETTLE_SECONDS)
    left = sorted({run["id"] for run in runs} & {run["id"] for run in queued(repo)})
    print(f"{len(left)} of {len(runs)} stale runs still queued")
    return left


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    args = parser.parse_args(argv)
    runs = stale(queued(args.repo), datetime.now(UTC))
    print(f"{len(runs)} stale queued runs")
    if not runs:
        return 0
    for run_id in clear(args.repo, runs):
        print(f"::warning::run {run_id} stays queued after the stale run sweep")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
