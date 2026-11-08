import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any, cast

from scripts.ci_budget import rate_limit

gh_api = cast("Callable[..., subprocess.CompletedProcess[bytes]]", rate_limit.gh_api)

RETRY_DELAYS = (5, 10, 20, None)
POLL_SECONDS = 15
WAIT_SECONDS = 300
NOT_LISTED = "not listed"
UNKNOWN = "unknown"


def api(endpoint: str, *, binary: bool = False) -> list[Any] | bytes:
    command = [endpoint]
    if not binary:
        command.extend(["--paginate", "--slurp"])
    result: subprocess.CompletedProcess[bytes] | None = None
    for delay in RETRY_DELAYS:
        try:
            result = gh_api(command, timeout=60)
            break
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            if delay is None:
                raise
            time.sleep(delay)
    assert result is not None
    return result.stdout if binary else json.loads(result.stdout)


def ready(jobs: list[dict[str, Any]], shards: list[int]) -> bool:
    passed = {job["name"] for job in jobs if job.get("conclusion") == "success"}
    return bool(shards) and all(
        f"unit (3.12, {shard})" in passed or f"unit rerun (3.12, {shard})" in passed for shard in shards
    )


def waiting(jobs: list[dict[str, Any]], shards: list[int]) -> list[str]:
    """Describe each planned shard with no successful unit or rerun job by the state of both jobs."""
    states: dict[str, list[str]] = {}
    for job in jobs:
        state: str = job.get("conclusion") or job.get("status") or UNKNOWN
        states.setdefault(str(job["name"]), []).append(state)
    pending: list[str] = []
    for shard in shards:
        unit = states.get(f"unit (3.12, {shard})", [NOT_LISTED])
        rerun = states.get(f"unit rerun (3.12, {shard})", [])
        if "success" in unit or "success" in rerun:
            continue
        described = f"shard {shard} unit {', '.join(unit)}"
        if rerun:
            described += f", rerun {', '.join(rerun)}"
        pending.append(described)
    return pending


def watch(repository: str, run: str, attempt: str, shards: list[int], *, recover: bool = True) -> None:
    """Poll the run's jobs until every planned shard passed, logging each poll, inside a fixed deadline."""
    units = {f"unit (3.12, {shard})" for shard in shards}
    start = time.monotonic()
    deadline = rate_limit.deadline = start + WAIT_SECONDS
    polls = 0
    pending: list[str] = []
    while (now := time.monotonic()) < deadline:
        polls += 1
        pages = cast(
            "list[dict[str, Any]]", api(f"repos/{repository}/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100")
        )
        jobs = [job for page in pages for job in page["jobs"]]
        pending = waiting(jobs, shards)
        print(
            f"Poll {polls} at {now - start:.0f} s: {len(shards) - len(pending)} of {len(shards)} planned shards"
            f" passed; waiting on {'; '.join(pending) or 'nothing'}",
            file=sys.stderr,
        )
        failures = [
            job["name"]
            for job in jobs
            if job["name"] in units and job.get("status") == "completed" and job.get("conclusion") != "success"
        ]
        if not recover and failures:
            raise RuntimeError(f"Original shards cannot recover on this event: {failures}")
        if ready(jobs, shards):
            return
        time.sleep(POLL_SECONDS)
    raise RuntimeError(
        f"Shard verification timed out after {WAIT_SECONDS} s and {polls} polls before every expected shard"
        f" succeeded; still waiting on {'; '.join(pending) or 'every planned shard'}"
    )


def main() -> None:
    rate_limit.budget("Shard verification", "start")
    try:
        watch(
            os.environ["GITHUB_REPOSITORY"],
            os.environ["GITHUB_RUN_ID"],
            os.environ["GITHUB_RUN_ATTEMPT"],
            json.loads(os.environ["SHARDS"]),
            recover=os.environ.get("GITHUB_EVENT_NAME") == "merge_group",
        )
    finally:
        rate_limit.budget("Shard verification", "end")
        rate_limit.report("Shard verification")


if __name__ == "__main__":
    main()
