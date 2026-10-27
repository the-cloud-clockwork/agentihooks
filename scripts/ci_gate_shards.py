import json
import os
import subprocess
import time

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


def watch(
    repository: str,
    run: str,
    attempt: str,
    shards: list[int],
    *,
    recover: bool = True,
    queue: tuple[str, str] | None = None,
) -> None:
    from scripts import ci_gate_queue

    units = {f"unit (3.12, {shard})" for shard in shards}
    reruns = {f"unit rerun (3.12, {shard})" for shard in shards}
    seen = {}
    deadline = rate_limit.deadline = time.monotonic() + 1200
    while time.monotonic() < deadline:
        if queue:
            ci_gate_queue.current(repository, *queue)
        pages = api(f"repos/{repository}/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100")
        jobs = [job for page in pages for job in page["jobs"]]
        if queue:
            if ci_gate_queue.verify(
                jobs, shards, lost=lambda job: ci_gate_queue.logged_loss(repository, job, seen), preliminary=True
            ):
                ci_gate_queue.current(repository, *queue)
                return
        else:
            failures = [
                job["name"]
                for job in jobs
                if job["name"] in units and job.get("status") == "completed" and job.get("conclusion") != "success"
            ]
            if not recover and failures:
                raise RuntimeError(f"Original shards cannot recover on this event: {failures}")
            if ready(jobs, shards):
                return
        if any(job["name"] in units | reruns and job.get("status") == "queued" for job in jobs):
            deadline = rate_limit.deadline = time.monotonic() + 1200
        time.sleep(POLL_SECONDS)
    raise RuntimeError("Shard verification timed out before every expected shard succeeded")


def main() -> None:
    rate_limit.budget("Shard verification", "start")
    try:
        watch(
            os.environ["GITHUB_REPOSITORY"],
            os.environ["GITHUB_RUN_ID"],
            os.environ["GITHUB_RUN_ATTEMPT"],
            json.loads(os.environ["SHARDS"]),
            recover=os.environ.get("GITHUB_EVENT_NAME") == "merge_group",
            queue=(os.environ["GITHUB_REF"], os.environ["GITHUB_SHA"])
            if os.environ.get("GITHUB_EVENT_NAME") == "merge_group"
            else None,
        )
    finally:
        rate_limit.budget("Shard verification", "end")
        rate_limit.report("Shard verification")


if __name__ == "__main__":
    main()
