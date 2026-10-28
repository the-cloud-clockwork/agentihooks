import argparse
import json
import subprocess
import time


def _api(endpoint: str) -> dict:
    return json.loads(
        subprocess.run(["gh", "api", endpoint], check=True, capture_output=True, text=True, timeout=30).stdout
    )


def verify(repository: str, head: str, attempts: int = 36, interval: float = 5, event: str = "pull_request") -> str:
    problem = "Tests run is missing for this head."
    for attempt in range(attempts):
        runs = _api(f"repos/{repository}/actions/workflows/test.yml/runs?head_sha={head}&event={event}&per_page=1")[
            "workflow_runs"
        ]
        matching = [run for run in runs if run["head_sha"] == head]
        if matching:
            run = matching[0]
            if run["status"] == "completed" and run["conclusion"] != "success":
                return f"Tests run {run['id']} ended with {run['conclusion']}."
            jobs = _api(f"repos/{repository}/actions/runs/{run['id']}/jobs?filter=latest&per_page=1")
            if jobs["total_count"] > 0:
                return ""
            problem = f"Tests run {run['id']} has no jobs."
        if attempt + 1 < attempts:
            time.sleep(interval)
    return problem


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--event", default="pull_request")
    args = parser.parse_args(argv)
    problem = verify(args.repository, args.head, event=args.event)
    if problem:
        print(f"::error::{problem}")
        return 1
    print("Tests startup verified for this head.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
