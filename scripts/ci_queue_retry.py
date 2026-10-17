import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path

import yaml

SHARD = "unit"
WORKFLOW = Path(".github/workflows/test.yml")
RUNNER_LOSS = re.compile(
    r"The runner has received a shutdown signal"
    r"|lost communication with the server"
    r"|The job was not acquired by Runner"
)
QUEUE_BRANCH = re.compile(r"^gh-readonly-queue/.+/pr-(\d+)-[0-9a-f]+$")
EXIT_POLLS = 12
EXIT_POLL_SECONDS = 15


def base_name(job_name: str) -> str:
    return job_name.split(" (")[0].strip()


def dependents(workflow: dict, root: str = SHARD) -> set[str]:
    jobs = workflow["jobs"]

    def needs(job_id: str) -> set[str]:
        value = jobs[job_id].get("needs", [])
        return {value} if isinstance(value, str) else set(value)

    found: set[str] = set()
    for _ in jobs:
        found.update({job_id for job_id in jobs if needs(job_id) & (found | {root})})
    return {jobs[job_id].get("name", job_id) for job_id in found}


def decide(run: dict, failed: list[dict], workflow: dict) -> tuple[bool, str]:
    if run.get("event") != "merge_group" or run.get("conclusion") not in ("failure", "cancelled"):
        return False, "not a failed merge group run"
    if run.get("run_attempt") != 1:
        return False, f"attempt {run.get('run_attempt')} already ran"
    downstream = dependents(workflow)
    shards = [job for job in failed if base_name(job["name"]) == SHARD]
    others = [job["name"] for job in failed if base_name(job["name"]) not in downstream | {SHARD}]
    if others:
        return False, f"failed outside the unit shards: {', '.join(others)}"
    if not shards:
        return False, "no unit shard failed"
    kept = [job["name"] for job in shards if not RUNNER_LOSS.search(job["text"])]
    if kept:
        return False, f"failed without a runner loss signature: {', '.join(kept)}"
    return True, f"runner lost on {', '.join(job['name'] for job in shards)}"


def _api(*args: str) -> str:
    return subprocess.run(["gh", "api", *args], stdout=subprocess.PIPE, text=True, check=True).stdout


def failed_jobs(repo: str, run_id: int) -> list[dict]:
    jobs = json.loads(_api(f"repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100"))
    failed = []
    for job in jobs["jobs"]:
        if job["conclusion"] != "failure":
            continue
        notes = [note["message"] for note in json.loads(_api(f"repos/{repo}/check-runs/{job['id']}/annotations"))]
        if base_name(job["name"]) == SHARD:
            log = _api(f"repos/{repo}/actions/jobs/{job['id']}/logs", "--allow-escape-sequences")
            notes += [line for line in log.splitlines() if "##[error]" in line]
        failed.append({"name": job["name"], "text": "\n".join(notes)})
    unlisted = jobs["total_count"] - len(jobs["jobs"])
    if unlisted > 0:
        failed.append({"name": f"{unlisted} unlisted jobs", "text": ""})
    return failed


def pull_number(run: dict) -> int | None:
    match = QUEUE_BRANCH.search(str(run.get("head_branch")))
    return int(match.group(1)) if match else None


def requeue(pull: dict) -> tuple[bool, str]:
    if pull.get("state") != "OPEN" or pull.get("isInMergeQueue"):
        return False, "pull request is not open outside the queue"
    head_at = pull["commits"]["nodes"][-1]["commit"]["committedDate"]
    since_head = [event for event in pull["timelineItems"]["nodes"] if event["createdAt"] >= head_at]
    if since_head and since_head[-1]["reason"] != "failed_checks":
        return False, f"head last left the queue for {since_head[-1]['reason']}"
    removals = [event for event in since_head if event["reason"] == "failed_checks"]
    if len(removals) > 1:
        return False, f"head already left the queue {len(removals)} times on failed checks"
    return True, "first failed checks removal of this head"


def pull_state(repo: str, number: int) -> dict:
    owner, name = repo.split("/")
    query = (
        f'query{{repository(owner:"{owner}",name:"{name}"){{pullRequest(number:{number}){{'
        "id state isInMergeQueue commits(last:1){nodes{commit{committedDate}}} "
        "timelineItems(last:100,itemTypes:[REMOVED_FROM_MERGE_QUEUE_EVENT])"
        "{nodes{... on RemovedFromMergeQueueEvent{createdAt reason}}}}}}"
    )
    return json.loads(_api("graphql", "-f", f"query={query}"))["data"]["repository"]["pullRequest"]


def pull_after_exit(repo: str, number: int) -> dict:
    pull = pull_state(repo, number)
    for _ in range(EXIT_POLLS):
        if not pull.get("isInMergeQueue"):
            return pull
        time.sleep(EXIT_POLL_SECONDS)
        pull = pull_state(repo, number)
    return pull


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    args = parser.parse_args(argv)
    run = json.loads(_api(f"repos/{args.repo}/actions/runs/{args.run_id}"))
    retry, reason = decide(run, failed_jobs(args.repo, args.run_id), yaml.safe_load(WORKFLOW.read_text()))
    number = pull_number(run)
    if retry and number is None:
        retry, reason = False, f"no pull request in queue branch {run.get('head_branch')}"
    if retry:
        allowed, why = requeue(pull_after_exit(args.repo, number))
        retry, reason = allowed, f"{reason}; {why}"
    print(json.dumps({"run": args.run_id, "pull": number, "requeue": retry, "reason": reason}))
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a") as handle:
            handle.write(f"requeue={str(retry).lower()}\npull={number or ''}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
