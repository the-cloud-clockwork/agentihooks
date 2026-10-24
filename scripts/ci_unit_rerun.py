import argparse
import json
import os
import re
from collections.abc import Callable
from pathlib import Path

from scripts.ci_queue_fail_fast import logged_loss
from scripts.ci_queue_retry import SHARD, base_name

LEG = re.compile(r"\((\S+), (\d+)\)$")
FAILED = ("failure", "cancelled", "timed_out")


def classify(jobs: list[dict], lost: Callable[[dict], bool]) -> tuple[list[tuple[str, int]], list[str]]:
    legs, kept = [], []
    for job in jobs:
        if base_name(job["name"]) != SHARD or job.get("conclusion") not in FAILED:
            continue
        match = LEG.search(job["name"])
        if match and job["conclusion"] != "timed_out" and lost(job):
            legs.append((match[1], int(match[2])))
        else:
            kept.append(job["name"])
    return legs, kept


def decide(jobs: list[dict], plan: list[dict], lost: Callable[[dict], bool]) -> tuple[list[dict], str]:
    legs, kept = classify(jobs, lost)
    if kept:
        return [], f"failed without a runner loss signature: {', '.join(kept)}"
    if not legs:
        return [], "no unit shard lost its runner"
    matrix = [leg for leg in plan if (str(leg["python-version"]), int(leg["shard"])) in legs]
    if len(matrix) != len(legs):
        return [], f"the queue plan lacks a lost leg: {legs}"
    return matrix, f"rerunning {len(matrix)} lost unit shards once"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", type=Path, required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--plan", required=True)
    args = parser.parse_args(argv)
    jobs = [json.loads(line) for line in args.jobs.read_text().splitlines() if line]
    seen: dict[str, bool] = {}
    matrix, reason = decide(jobs, json.loads(f"[{args.plan}]"), lambda job: logged_loss(args.repo, job, seen))
    print(json.dumps({"rerun": matrix, "reason": reason}))
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a") as handle:
            handle.write(f"matrix={json.dumps(matrix)}\n")
    return 0 if matrix else 1


if __name__ == "__main__":
    raise SystemExit(main())
