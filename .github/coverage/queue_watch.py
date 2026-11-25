"""Write whether this run's shard jobs have started, so the coverage wait can skip queued time.

arrivals.py takes no GitHub token and makes no API call, so it cannot read job states itself. This
runs beside it, with the App token the "Mint the token" step readies, and leaves its answer in
STATE as plain text for arrivals.py to read with a file stat. BOUND stays under that token's one
hour lifetime: past it every poll fails authorization and started() reads as unknown, so the
state file goes stale and arrive() falls back to spending the budget as if the shard were
running. A shard already completed never holds the answer back; among the rest, a job is queued
when the API reports its status so, and the scope has started once none of them is.
"""

import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

INTERVAL = 30
BOUND = 55 * 60
TIMEOUT = 30
STATE = ".sonar-shard-queue-state"


def queued(job: dict) -> bool:
    """Say whether GitHub has not yet handed this job a runner."""
    return job.get("status") == "queued"


def completed(job: dict) -> bool:
    """Say whether GitHub has finished running this job."""
    return job.get("status") == "completed"


def started(api: str, repo: str, run_id: str, attempt: str, token: str, prefix: str) -> bool | None:
    """Say whether a shard job of this scope is running or done, or None when that could not be read."""
    request = urllib.request.Request(
        f"{api}/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            payload = json.loads(response.read() or b"{}")
    except (OSError, ValueError):
        return None
    shards = [job for job in payload.get("jobs") or [] if str(job.get("name", "")).startswith(prefix)]
    if not shards:
        return None
    remaining = [job for job in shards if not completed(job)]
    return True if not remaining else all(not queued(job) for job in remaining)


def history(api: str, repo: str, run_id: str, token: str) -> list[dict[str, Any]] | None:
    found = []
    page = 1
    while True:
        request = urllib.request.Request(
            f"{api}/repos/{repo}/actions/runs/{run_id}/jobs?filter=all&per_page=100&page={page}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                payload = json.loads(response.read() or b"{}")
        except (OSError, ValueError):
            return None
        jobs = payload.get("jobs") or []
        found.extend(jobs)
        if len(found) >= payload.get("total_count", len(found)):
            return found
        if not jobs:
            return None
        page += 1


def raw_state(jobs: list[dict[str, Any]], attempt: int, prefix: str, shards: int) -> tuple[bool | None, bool]:
    expected = {f"{prefix}3.12, {shard})" for shard in range(1, shards + 1)}
    latest = {
        job["name"]: job
        for job in sorted(jobs, key=lambda job: job["id"])
        if job["name"] in expected and int(job["run_attempt"]) <= attempt
    }
    if not latest:
        return None, False
    upstream = any(
        int(job["run_attempt"]) == attempt and job["name"] in {"select", "split", "reuse", "admission", "lab-verdict"}
        for job in jobs
    )
    materialized = not upstream or all(int(job["run_attempt"]) == attempt for job in latest.values())
    complete = (
        set(latest) == expected
        and materialized
        and all(completed(job) and job.get("conclusion") == "success" for job in latest.values())
    )
    remaining = [job for job in latest.values() if not completed(job)]
    return materialized and all(not queued(job) for job in remaining), complete


def main() -> int:
    """Poll this run attempt's shard jobs and keep STATE current until the bound runs out."""
    token = os.environ["GH_APP_TOKEN"]
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    repo = os.environ["GITHUB_REPOSITORY"]
    run_id = os.environ["GITHUB_RUN_ID"]
    attempt = os.environ["GITHUB_RUN_ATTEMPT"]
    prefix = sys.argv[1]
    raw = os.environ.get("COVERAGE_RAW") == "true"
    deadline = time.monotonic() + BOUND
    while time.monotonic() < deadline:
        if raw:
            jobs = history(api, repo, run_id, token)
            state, complete = (
                (None, False) if jobs is None else raw_state(jobs, int(attempt), prefix, int(os.environ["SHARDS"]))
            )
            Path(STATE + ".complete").write_text(
                json.dumps(
                    {
                        "run": run_id,
                        "attempt": int(attempt),
                        "complete": complete,
                    }
                )
            )
        else:
            state = started(api, repo, run_id, attempt, token, prefix)
        if state is not None:
            Path(STATE).write_text("started" if state else "queued")
        time.sleep(INTERVAL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
