import argparse
import json
import os
import re
import subprocess
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


def base_name(job_name: str) -> str:
    return job_name.split(" (")[0].strip()


def dependents(workflow: dict, root: str = SHARD) -> set[str]:
    jobs = workflow.get("jobs") or {}

    def needs(job_id: str) -> set[str]:
        value = jobs[job_id].get("needs") or []
        return {value} if isinstance(value, str) else set(value)

    found: set[str] = set()
    grew = True
    while grew:
        grew = False
        for job_id in jobs:
            if job_id not in found and needs(job_id) & (found | {root}):
                found.add(job_id)
                grew = True
    return {jobs[job_id].get("name", job_id) for job_id in found}


def decide(run: dict, failed: list[dict], workflow: dict) -> tuple[bool, str]:
    if run.get("event") != "merge_group" or run.get("conclusion") != "failure":
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
    kept = [job["name"] for job in shards if not RUNNER_LOSS.search(job.get("text", ""))]
    if kept:
        return False, f"failed without a runner loss signature: {', '.join(kept)}"
    return True, f"runner lost on {', '.join(job['name'] for job in shards)}"


def _api(*args: str) -> str:
    done = subprocess.run(["gh", "api", *args], capture_output=True, text=True, check=False)
    return done.stdout if done.returncode == 0 else ""


def failed_jobs(repo: str, run_id: int) -> list[dict]:
    jobs = json.loads(_api(f"repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100") or "{}")
    failed = []
    for job in jobs.get("jobs", []):
        if job.get("conclusion") != "failure":
            continue
        annotations = json.loads(_api(f"repos/{repo}/check-runs/{job['id']}/annotations") or "[]")
        text = "\n".join(note.get("message", "") for note in annotations)
        if base_name(job["name"]) == SHARD:
            log = _api(f"repos/{repo}/actions/jobs/{job['id']}/logs")
            text += "\n" + "\n".join(line for line in log.splitlines() if "##[error]" in line)
        failed.append({"name": job["name"], "text": text})
    return failed


def pull_number(run: dict) -> int | None:
    match = QUEUE_BRANCH.search(run.get("head_branch") or "")
    return int(match.group(1)) if match else None


def requeue(pull: dict) -> tuple[bool, str]:
    if pull.get("state") != "OPEN" or pull.get("isInMergeQueue"):
        return False, "pull request is not open outside the queue"
    head_at = pull["commits"]["nodes"][-1]["commit"]["committedDate"]
    removals = [
        event
        for event in pull["timelineItems"]["nodes"]
        if event.get("reason") == "failed_checks" and event.get("createdAt", "") >= head_at
    ]
    if len(removals) > 1:
        return False, f"head already left the queue {len(removals)} times on failed checks"
    return True, "first failed checks removal of this head"


def pull_state(repo: str, number: int) -> dict:
    owner, name = repo.split("/")
    query = (
        f'query{{repository(owner:"{owner}",name:"{name}"){{pullRequest(number:{number}){{'
        "id state isInMergeQueue commits(last:1){nodes{commit{committedDate}}} "
        "timelineItems(last:100,itemTypes:[REMOVED_FROM_MERGE_QUEUE_EVENT])"
        "{nodes{... on RemovedFromMergeQueueEvent{createdAt reason}}}}}}}"
    )
    answer = json.loads(_api("graphql", "-f", f"query={query}") or "{}")
    return ((answer.get("data") or {}).get("repository") or {}).get("pullRequest") or {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    args = parser.parse_args(argv)
    run = json.loads(_api(f"repos/{args.repo}/actions/runs/{args.run_id}") or "{}")
    retry, reason = decide(run, failed_jobs(args.repo, args.run_id), yaml.safe_load(WORKFLOW.read_text()))
    number = pull_number(run)
    if retry and number is None:
        retry, reason = False, f"no pull request in queue branch {run.get('head_branch')}"
    if retry:
        allowed, why = requeue(pull_state(args.repo, number))
        retry, reason = allowed, f"{reason}; {why}"
    print(json.dumps({"run": args.run_id, "pull": number, "requeue": retry, "reason": reason}))
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a") as handle:
            handle.write(f"requeue={str(retry).lower()}\npull={number or ''}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
