import argparse
import json
from pathlib import Path

from scripts.ci_queue_retry import SHARD, base_name

SELF = "queue-fail-fast"
# Needs every graded job, so once it completes nothing graded can still fail.
LAST = "stage-budget"
FATAL = ("failure", "timed_out", "cancelled")


def failed(jobs: list[dict]) -> str | None:
    return next(
        (
            job["name"]
            for job in jobs
            if job["name"] != SELF and base_name(job["name"]) != SHARD and job.get("conclusion") in FATAL
        ),
        None,
    )


def settled(jobs: list[dict]) -> bool:
    return any(job["name"] == LAST and job["status"] == "completed" for job in jobs)


def verdict(jobs: list[dict]) -> str:
    if name := failed(jobs):
        return f"failed={name}"
    return "failed=" if settled(jobs) else "wait"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("jobs", type=Path)
    args = parser.parse_args(argv)
    print(verdict([json.loads(line) for line in args.jobs.read_text().splitlines() if line]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
