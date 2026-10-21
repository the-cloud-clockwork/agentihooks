import argparse
import json
import subprocess
from collections.abc import Callable
from pathlib import Path

from scripts.ci_queue_retry import RUNNER_LOSS, SHARD, base_name, log_errors

SELF = "queue-fail-fast"
# Needs every graded job, so once it completes nothing graded can still fail.
LAST = "stage-budget"
FATAL = ("failure", "timed_out", "cancelled")


def failed(jobs: list[dict], lost: Callable[[dict], bool] = lambda job: False) -> str | None:
    return next(
        (
            job["name"]
            for job in jobs
            if job["name"] != SELF
            and job.get("conclusion") in FATAL
            and not (base_name(job["name"]) == SHARD and lost(job))
        ),
        None,
    )


def settled(jobs: list[dict]) -> bool:
    return any(job["name"] == LAST and job["status"] == "completed" for job in jobs)


def logged_loss(repo: str, job: dict) -> bool:
    # A missing log is how GitHub often reports a lost runner; unit-lost then reads the annotations.
    try:
        return bool(RUNNER_LOSS.search("\n".join(log_errors(repo, job["id"]))))
    except subprocess.CalledProcessError:
        return True


def verdict(jobs: list[dict], lost: Callable[[dict], bool] = lambda job: False) -> str:
    if name := failed(jobs, lost):
        return f"failed={name}"
    return "failed=" if settled(jobs) else "wait"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("jobs", type=Path)
    parser.add_argument("--repo")
    args = parser.parse_args(argv)
    jobs = [json.loads(line) for line in args.jobs.read_text().splitlines() if line]
    print(verdict(jobs, lambda job: logged_loss(args.repo, job)) if args.repo else verdict(jobs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
