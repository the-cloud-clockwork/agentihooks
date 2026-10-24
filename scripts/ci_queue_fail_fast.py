import argparse
import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from scripts.ci_queue_retry import RUNNER_LOSS, SHARD, STEP_EXITED, base_name, log_errors

SELF = "queue-fail-fast"
# Needs every graded job, so once it completes nothing graded can still fail.
LAST = "stage-budget"
FATAL = ("failure", "timed_out", "cancelled")
EXITED = re.compile(r"##\[error\]" + STEP_EXITED.pattern)


def failed(jobs: list[dict], lost: Callable[[dict], bool] = lambda job: False) -> str | None:
    return next(
        (
            job["name"]
            for job in jobs
            if job["name"] != SELF
            and job.get("conclusion") in FATAL
            and not (base_name(job["name"]) == SHARD and job["conclusion"] != "timed_out" and lost(job))
        ),
        None,
    )


def settled(jobs: list[dict]) -> bool:
    return any(job["name"] == LAST and job["status"] == "completed" for job in jobs)


def logged_loss(repo: str, job: dict, seen: dict[str, bool]) -> bool:
    # A lost runner may leave no log or only an annotation; only a step that exited nonzero proves a real failure.
    key = str(job["id"])
    if key not in seen:
        try:
            errors = log_errors(repo, job["id"])
        except subprocess.CalledProcessError:
            return True
        lost = any(RUNNER_LOSS.search(line) for line in errors)
        seen[key] = lost or not any(EXITED.search(line) for line in errors)
    return seen[key]


def verdict(jobs: list[dict], lost: Callable[[dict], bool] = lambda job: False) -> str:
    if name := failed(jobs, lost):
        return f"failed={name}"
    return "failed=" if settled(jobs) else "wait"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("jobs", type=Path)
    parser.add_argument("--repo")
    parser.add_argument("--seen", type=Path)
    args = parser.parse_args(argv)
    jobs = [json.loads(line) for line in args.jobs.read_text().splitlines() if line]
    if not args.repo:
        print(verdict(jobs))
        return 0
    seen = json.loads(args.seen.read_text()) if args.seen and args.seen.exists() else {}
    print(verdict(jobs, lambda job: logged_loss(args.repo, job, seen)))
    if args.seen:
        args.seen.write_text(json.dumps(seen))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
