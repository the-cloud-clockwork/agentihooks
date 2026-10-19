import argparse
import json
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from scripts.ci_queue_retry import _api, base_name, dependents

WORKFLOWS = Path(".github/workflows")
OPEN = ("in_progress", "queued", "pending", "waiting", "requested")
RED = ("failure", "timed_out")
HOSTED_GROUP = "GitHub Actions"
ONLINE = '.runners[] | select(.status == "online") | .name'
RUNS = ".workflow_runs[]"
JOBS = ".jobs[]"
GRACE_MINUTES = 10
CONFIRM_SECONDS = 120
CANCEL_POLLS = 18
POLL_SECONDS = 10


def live_runners(org: str) -> set[str]:
    names = _api("--paginate", f"orgs/{org}/actions/runners?per_page=100", "--jq", ONLINE)
    return set(names.split())


def lost_jobs(jobs: list[dict], runners: set[str], now: datetime, grace: timedelta) -> list[dict]:
    return [
        job
        for job in jobs
        if job["status"] == "in_progress"
        and job.get("runner_name")
        and job["runner_name"] not in runners
        and job.get("runner_group_name") != HOSTED_GROUP
        and now - datetime.fromisoformat(job["started_at"]) >= grace
    ]


def job_ids(workflow: dict) -> dict[str, str]:
    return {base_name(spec.get("name", job_id)): job_id for job_id, spec in workflow["jobs"].items()}


def stalled(jobs: list[dict], lost: list[dict], workflow: dict) -> tuple[bool, str]:
    if not lost:
        return False, "no job lost its runner"
    ids = job_ids(workflow)
    names = {base_name(job["name"]) for job in lost}
    waiting = set().union(*(dependents(workflow, ids[name]) for name in names if name in ids))
    blocked = {base_name(name) for name in waiting}
    for job in jobs:
        if job in lost or job["status"] == "completed":
            continue
        if job["status"] == "in_progress":
            return False, f"{job['name']} still runs on a live runner"
        if base_name(job["name"]) not in blocked:
            return False, f"{job['name']} still waits for a runner"
    return True, f"runner lost on {', '.join(job['name'] for job in lost)}"


def decide(run: dict, jobs: list[dict]) -> tuple[str, str]:
    failed = [job["name"] for job in jobs if job.get("conclusion") in RED]
    if failed:
        return "cancel", f"failed on its own: {', '.join(failed)}"
    if run["run_attempt"] != 1:
        return "cancel", f"attempt {run['run_attempt']} lost its runner again"
    return "rerun", "first attempt lost its runner"


def run_state(repo: str, run_id: int) -> dict:
    return json.loads(_api(f"repos/{repo}/actions/runs/{run_id}"))


def stop(repo: str, run_id: int) -> dict:
    for action in ("cancel", "force-cancel"):
        _api("-X", "POST", f"repos/{repo}/actions/runs/{run_id}/{action}")
        for _ in range(CANCEL_POLLS):
            time.sleep(POLL_SECONDS)
            run = run_state(repo, run_id)
            if run["status"] == "completed":
                return run
    return run


def jobs_of(repo: str, run_id: int) -> list[dict]:
    url = f"repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100"
    return [json.loads(line) for line in _api("--paginate", url, "--jq", JOBS).splitlines()]


def open_runs(repo: str, workflow: str) -> list[dict]:
    runs = []
    for status in OPEN:
        url = f"repos/{repo}/actions/workflows/{workflow}/runs?per_page=100&status={status}"
        runs += [json.loads(line) for line in _api("--paginate", url, "--jq", RUNS).splitlines()]
    return runs


def assess(repo: str, run: dict, workflow: dict, grace: timedelta) -> tuple[bool, str, list[dict]]:
    jobs = jobs_of(repo, run["id"])
    lost = lost_jobs(jobs, live_runners(repo.split("/")[0]), datetime.now(UTC), grace)
    stuck, reason = stalled(jobs, lost, workflow)
    return stuck, reason, jobs


def report(run: dict, action: str, reason: str) -> dict:
    return {"run": run["id"], "attempt": run["run_attempt"], "action": action, "reason": reason}


def first_look(repo: str, run: dict, workflow: dict, grace: timedelta) -> dict | None:
    if run["status"] == "completed":
        return report(run, "none", f"run completed with conclusion {run['conclusion']}")
    stuck, reason, _ = assess(repo, run, workflow, grace)
    return None if stuck else report(run, "none", reason)


def watch(repo: str, run: dict, workflow: dict, grace: timedelta) -> dict:
    stuck, reason, jobs = assess(repo, run, workflow, grace)
    if not stuck:
        return report(run, "none", reason)
    action, why = decide(run, jobs)
    if run_state(repo, run["id"])["run_attempt"] != run["run_attempt"]:
        return report(run, "none", "the run was rerun meanwhile")
    try:
        ended = stop(repo, run["id"])
    except subprocess.CalledProcessError:
        ended = run_state(repo, run["id"])
        if ended["status"] != "completed":
            raise
        if ended["conclusion"] != "cancelled":
            return report(run, "none", "the run completed meanwhile")
    if ended["status"] != "completed":
        return report(run, "cancel", f"{reason}; the run did not end after force cancel")
    if action == "cancel":
        return report(run, "cancel", f"{reason}; {why}")
    _api("-X", "POST", f"repos/{repo}/actions/runs/{run['id']}/rerun-failed-jobs")
    return report(run, "rerun", f"{reason}; {why}")


def guarded(step, repo: str, run: dict, workflow: dict, grace: timedelta) -> dict | None:
    try:
        return step(repo, run, workflow, grace)
    except subprocess.CalledProcessError as error:
        return report(run, "error", str(error))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--workflow", default="test.yml")
    parser.add_argument("--run-id", action="append", default=[])
    parser.add_argument("--grace-minutes", type=float, default=GRACE_MINUTES)
    args = parser.parse_args(argv)
    workflow = yaml.safe_load((WORKFLOWS / args.workflow).read_text())
    grace = timedelta(minutes=args.grace_minutes)
    runs = [run_state(args.repo, run_id) for run_id in args.run_id] or open_runs(args.repo, args.workflow)
    reports, suspects = [], []
    for run in runs:
        found = guarded(first_look, args.repo, run, workflow, grace)
        if found:
            reports.append(found)
            print(json.dumps(found))
        else:
            suspects.append(run)
    if suspects:
        time.sleep(CONFIRM_SECONDS)
    for run in suspects:
        found = guarded(watch, args.repo, run, workflow, grace)
        reports.append(found)
        print(json.dumps(found))
    return 1 if any(found["action"] == "error" for found in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
