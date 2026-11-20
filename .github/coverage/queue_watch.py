"""Write whether this run's shard jobs have started, so the coverage wait can skip queued time.

arrivals.py takes no GitHub token and makes no API call, so it cannot read job states itself. This
runs beside it, with the App token the "Mint the token" step readies, and leaves its answer in
STATE as plain text for arrivals.py to read with a file stat. A job is still queued when GitHub has
not yet handed it a runner: its runner_name is empty and started_at still equals created_at.
"""

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

INTERVAL = 30
BOUND = 65 * 60
TIMEOUT = 30
STATE = ".sonar-shard-queue-state"


def queued(job: dict) -> bool:
    """Say whether GitHub has not yet handed this job a runner."""
    return not job.get("runner_name") and job.get("started_at") == job.get("created_at")


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
    return any(not queued(job) for job in shards)


def main() -> int:
    """Poll this run attempt's shard jobs and keep STATE current until the bound runs out."""
    token = os.environ["GH_APP_TOKEN"]
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    repo = os.environ["GITHUB_REPOSITORY"]
    run_id = os.environ["GITHUB_RUN_ID"]
    attempt = os.environ["GITHUB_RUN_ATTEMPT"]
    prefix = sys.argv[1]
    deadline = time.monotonic() + BOUND
    while time.monotonic() < deadline:
        state = started(api, repo, run_id, attempt, token, prefix)
        if state is not None:
            Path(STATE).write_text("started" if state else "queued")
        time.sleep(INTERVAL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
