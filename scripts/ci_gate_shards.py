import json
import os
import subprocess
import sys
import time
from pathlib import Path

from scripts.ci_budget import rate_limit
from scripts.ci_budget.rate_limit import gh_api

RETRY_DELAYS = (5, 10, 20, None)
POLL_SECONDS = 15


def api(endpoint: str, *, binary: bool = False) -> list | bytes:
    command = [endpoint]
    if not binary:
        command.extend(["--paginate", "--slurp"])
    for delay in RETRY_DELAYS:
        try:
            result = gh_api(command, timeout=60)
            break
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            if delay is None:
                raise
            time.sleep(delay)
    return result.stdout if binary else json.loads(result.stdout)


def ready(jobs: list[dict], shards: list[int]) -> bool:
    passed = {job["name"] for job in jobs if job.get("conclusion") == "success"}
    return bool(shards) and all(
        f"unit (3.12, {shard})" in passed or f"unit rerun (3.12, {shard})" in passed for shard in shards
    )


def watch(repository: str, run: str, attempt: str, shards: list[int], *, recover: bool = True) -> None:
    units = {f"unit (3.12, {shard})" for shard in shards}
    deadline = rate_limit.deadline = time.monotonic() + 1200
    while time.monotonic() < deadline:
        pages = api(f"repos/{repository}/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100")
        jobs = [job for page in pages for job in page["jobs"]]
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
    raise RuntimeError("Shard verification timed out before every expected shard succeeded")


def reports(destination: Path, shards: list[int]) -> None:
    expected = {f"{kind}-3.12-{shard}" for kind in ("durations", "flakes") for shard in shards}
    present = {path.name for path in destination.iterdir() if path.is_dir()} if destination.is_dir() else set()
    if missing := expected - present:
        raise RuntimeError(f"Missing shard reports: {sorted(missing)}")


def main() -> None:
    shards = json.loads(os.environ["SHARDS"])
    if sys.argv[1:] == ["reports"]:
        reports(Path("shard-reports"), shards)
        return
    try:
        watch(
            os.environ["GITHUB_REPOSITORY"],
            os.environ["GITHUB_RUN_ID"],
            os.environ["GITHUB_RUN_ATTEMPT"],
            shards,
            recover=os.environ.get("GITHUB_EVENT_NAME") == "merge_group",
        )
    finally:
        rate_limit.report("Shard verification")


if __name__ == "__main__":
    main()
