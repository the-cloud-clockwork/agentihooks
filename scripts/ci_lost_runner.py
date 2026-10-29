import argparse
import fcntl
import json
import subprocess
import time
from collections.abc import Callable, Container
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO
from urllib.parse import quote

import yaml

from scripts.ci_queue_retry import _api, base_name, dependents, job_names, polling_jobs

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
SCALE_SET = "actions.github.com/scale-set-name"
NODE_BOUND_SECONDS = 60
CLUSTER_CONFIRM_SECONDS = 30
Source = Callable[[str], Container[str]]


def live_runners(org: str) -> set[str]:
    names = _api("--paginate", f"orgs/{org}/actions/runners?per_page=100", "--jq", ONLINE)
    return set(names.split())


class ClusterRunners:
    def __init__(self, sets: set[str], pods: list[dict], nodes: list[dict], now: datetime, bound: timedelta):
        away = {node["metadata"]["name"]: _not_ready_for(node, now) for node in nodes}
        self.sets = sets
        self.live = {pod["metadata"]["name"] for pod in pods if _alive(pod, away, bound)}

    def __contains__(self, name: str) -> bool:
        return name in self.live or name.rpartition("-runner-")[0].rpartition("-")[0] not in self.sets


def _not_ready_for(node: dict, now: datetime) -> timedelta | None:
    ready = next((c for c in node["status"].get("conditions", []) if c["type"] == "Ready"), None)
    if not ready or ready["status"] == "True":
        return None
    return now - datetime.fromisoformat(ready["lastTransitionTime"])


def _alive(pod: dict, away: dict[str, timedelta | None], bound: timedelta) -> bool:
    node = pod["spec"].get("nodeName")
    phase = pod["status"].get("phase")
    runner = [c for c in pod["status"].get("containerStatuses", []) if c["name"] == "runner"]
    if (node and node not in away) or phase in ("Failed", "Succeeded"):
        return False
    if any("terminated" in c["state"] for c in runner):
        return False
    down_for = away.get(node)
    return phase != "Unknown" if down_for is None else down_for < bound


def _kubectl(*args: str) -> list[dict]:
    command = ["kubectl", "get", *args, "-o", "json"]
    return json.loads(subprocess.run(command, check=True, capture_output=True, timeout=60).stdout)["items"]


def cluster_runners(_org: str) -> ClusterRunners:
    sets = {item["metadata"]["name"] for item in _kubectl("autoscalingrunnersets", "--all-namespaces")}
    pods = _kubectl("pods", "--all-namespaces", "-l", SCALE_SET)
    return ClusterRunners(sets, pods, _kubectl("nodes"), datetime.now(UTC), timedelta(seconds=NODE_BOUND_SECONDS))


def lost_jobs(jobs: list[dict], runners: set[str], now: datetime, grace: timedelta) -> list[dict]:
    return [
        job
        for job in jobs
        if job["status"] == "in_progress"
        and job.get("runner_name")
        and job["runner_name"] not in runners
        and job.get("runner_group_name") != HOSTED_GROUP
        and now - datetime.fromisoformat(job["started_at"]) >= grace
        and not _ran_every_step(job)
    ]


def _ran_every_step(job: dict) -> bool:
    steps = job.get("steps") or []
    return bool(steps) and all(step["status"] == "completed" for step in steps)


def job_ids(workflow: dict) -> dict[str, str]:
    return {base_name(name): job_id for job_id, name in job_names(workflow).items()}


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
        if job["status"] == "in_progress" and base_name(job["name"]) not in polling_jobs(workflow):
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


def stop(repo: str, run_id: int, recovery: dict) -> dict:
    for action, field in (("cancel", "cancel_requested_at"), ("force-cancel", "force_cancel_requested_at")):
        recovery[field] = datetime.now(UTC).isoformat()
        _api("-X", "POST", f"repos/{repo}/actions/runs/{run_id}/{action}")
        for _ in range(CANCEL_POLLS):
            time.sleep(POLL_SECONDS)
            run = run_state(repo, run_id)
            if run["status"] == "completed":
                recovery["completed_observed_at"] = datetime.now(UTC).isoformat()
                return run
    return run


def superseded(repo: str, run: dict) -> bool:
    if run.get("event") != "pull_request":
        return False
    branch = quote(run["head_branch"], safe="")
    url = f"repos/{repo}/actions/workflows/{run['workflow_id']}/runs?event=pull_request&branch={branch}&per_page=1"
    return json.loads(_api(url))["workflow_runs"][0]["id"] != run["id"]


def jobs_of(repo: str, run_id: int, cached: bool = False) -> list[dict]:
    url = f"repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100"
    if cached:
        from scripts.swarm.github_reads import conditional_pages

        return [job for page in conditional_pages("lost_runner.jobs", url, _api) for job in page["jobs"]]
    return [json.loads(line) for line in _api("--paginate", url, "--jq", JOBS).splitlines()]


def open_runs(repo: str, workflow: str) -> list[dict]:
    runs = []
    for status in OPEN:
        url = f"repos/{repo}/actions/workflows/{workflow}/runs?per_page=100&status={status}"
        runs += [json.loads(line) for line in _api("--paginate", url, "--jq", RUNS).splitlines()]
    return runs


def assess(
    repo: str, run: dict, workflow: dict, grace: timedelta, source: Source = live_runners, cached: bool = False
) -> tuple[bool, str, list[dict]]:
    jobs = jobs_of(repo, run["id"], cached)
    lost = lost_jobs(jobs, source(repo.split("/")[0]), datetime.now(UTC), grace)
    stuck, reason = stalled(jobs, lost, workflow)
    return stuck, reason, jobs


def report(run: dict, action: str, reason: str) -> dict:
    return {"run": run["id"], "attempt": run["run_attempt"], "action": action, "reason": reason}


def first_look(repo: str, run: dict, workflow: dict, grace: timedelta, source: Source = live_runners) -> dict | None:
    if run["status"] == "completed":
        return report(run, "none", f"run completed with conclusion {run['conclusion']}")
    stuck, reason, _ = assess(repo, run, workflow, grace, source, cached=True)
    return None if stuck else report(run, "none", reason)


def watch(repo: str, run: dict, workflow: dict, grace: timedelta, source: Source = live_runners) -> dict:
    stuck, reason, jobs = assess(repo, run, workflow, grace, source)
    if not stuck:
        return report(run, "none", reason)
    detected_at = datetime.now(UTC).isoformat()
    action, why = decide(run, jobs)
    if run_state(repo, run["id"])["run_attempt"] != run["run_attempt"]:
        return report(run, "none", "the run was rerun meanwhile")
    if superseded(repo, run):
        return report(run, "none", "a newer run of its pull request superseded it")
    recovery = report(run, action, f"{reason}; {why}") | {
        "detected_at": detected_at,
        "force_cancel_requested_at": None,
        "completed_observed_at": None,
    }
    try:
        try:
            ended = stop(repo, run["id"], recovery)
        except subprocess.CalledProcessError:
            ended = run_state(repo, run["id"])
            if ended["status"] != "completed":
                raise
            recovery["completed_observed_at"] = datetime.now(UTC).isoformat()
            if ended["conclusion"] != "cancelled":
                return recovery | {"action": "none", "reason": "the run completed meanwhile"}
        if ended["status"] != "completed":
            return recovery | {"action": "cancel", "reason": f"{reason}; the run did not end after force cancel"}
        if action == "rerun":
            _api("-X", "POST", f"repos/{repo}/actions/runs/{run['id']}/rerun-failed-jobs")
        return recovery
    except subprocess.SubprocessError as error:
        return recovery | {"action": "error", "reason": str(error)}


def guarded(step, repo: str, run: dict, workflow: dict, grace: timedelta, source: Source = live_runners) -> dict | None:
    try:
        return step(repo, run, workflow, grace, source)
    except subprocess.SubprocessError as error:
        return report(run, "error", str(error))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--workflow", default="test.yml")
    parser.add_argument("--run-id", action="append", default=[])
    parser.add_argument("--grace-minutes", type=float)
    parser.add_argument("--cluster", action="store_true")
    parser.add_argument("--lock")
    args = parser.parse_args(argv)
    held = _hold(args.lock) if args.lock else None
    if args.lock and held is None:
        print(json.dumps({"action": "none", "reason": "another watchdog holds the lock"}))
        return 0
    source = cluster_runners if args.cluster else live_runners
    confirm = CLUSTER_CONFIRM_SECONDS if args.cluster else CONFIRM_SECONDS
    minutes = args.grace_minutes
    if minutes is None:
        minutes = 0 if args.cluster else GRACE_MINUTES
    workflow = yaml.safe_load((WORKFLOWS / args.workflow).read_text())
    grace = timedelta(minutes=minutes)
    runs = [run_state(args.repo, run_id) for run_id in args.run_id] or open_runs(args.repo, args.workflow)
    reports, suspects = [], []
    for run in runs:
        found = guarded(first_look, args.repo, run, workflow, grace, source)
        if found:
            reports.append(found)
            print(json.dumps(found))
        else:
            suspects.append(run)
    if suspects:
        time.sleep(confirm)
    for run in suspects:
        found = guarded(watch, args.repo, run, workflow, grace, source)
        reports.append(found)
        print(json.dumps(found))
    return 1 if any(found["action"] == "error" for found in reports) else 0


def _hold(path: str) -> IO | None:
    handle = open(path, "a")  # noqa: SIM115
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


if __name__ == "__main__":
    raise SystemExit(main())
