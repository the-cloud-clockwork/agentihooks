import argparse
import json
import re
import subprocess
import time
from typing import Any

WORKFLOW = ".github/workflows/test.yml"
RUN_LINK = re.compile(r"/actions/runs/(\d+)/")


def _api(endpoint: str) -> dict[str, Any]:
    return json.loads(
        subprocess.run(["gh", "api", endpoint], check=True, capture_output=True, text=True, timeout=30).stdout
    )


def _checked_runs(repository: str, head: str, event: str, rejected: set[str]) -> list[dict[str, Any]]:
    """Return the head's newest Tests run named by its check runs, adding each other run read to ``rejected``."""
    checks = _api(f"repos/{repository}/commits/{head}/check-runs?per_page=100")["check_runs"]
    links = (RUN_LINK.search(check["details_url"]) for check in checks if check["details_url"])
    for run_id in sorted((link.group(1) for link in links if link), key=int, reverse=True):
        if run_id in rejected:
            continue
        run = _api(f"repos/{repository}/actions/runs/{run_id}")
        if run["path"] == WORKFLOW and run["event"] == event and run["head_sha"] == head:
            return [run]
        rejected.add(run_id)
    return []


def verify(
    repository: str,
    head: str,
    attempts: int = 36,
    interval: float = 5,
    event: str = "pull_request",
    waits: int = 36,
) -> str:
    """Wait for the head's Tests run to list a job, polling a queued or running run up to ``waits`` times.

    The run comes from the run listing, or from the head's check runs while the listing omits it.
    """
    problem = "Tests run is missing for this head."
    rejected: set[str] = set()
    for poll in range(max(attempts, waits)):
        runs = _api(f"repos/{repository}/actions/workflows/test.yml/runs?head_sha={head}&event={event}&per_page=1")[
            "workflow_runs"
        ]
        matching = [run for run in runs if run["head_sha"] == head] or _checked_runs(repository, head, event, rejected)
        if matching:
            run = matching[0]
            completed = run["status"] == "completed"
            if completed and run["conclusion"] != "success":
                return f"Tests run {run['id']} ended with {run['conclusion']}."
            jobs = _api(f"repos/{repository}/actions/runs/{run['id']}/jobs?filter=latest&per_page=1")
            if jobs["total_count"] > 0:
                return ""
            problem = f"Tests run {run['id']} has no jobs."
            if completed or poll + 1 >= waits:
                return problem
        elif poll + 1 >= attempts:
            return problem
        time.sleep(interval)
    return problem


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--event", default="pull_request")
    parser.add_argument("--waits", type=int, default=36)
    args = parser.parse_args(argv)
    problem = verify(args.repository, args.head, attempts=args.waits, event=args.event, waits=args.waits)
    if problem:
        print(f"::error::{problem}")
        return 1
    print("Tests startup verified for this head.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
