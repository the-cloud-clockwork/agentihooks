from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from scripts.doctor import ci, ci_cache
from scripts.swarm import github_reads
from scripts.swarm.keyspace import ROOT

if TYPE_CHECKING:
    from redis import Redis

TEST = re.compile(
    r"(?:(PASSED|FAILED|SKIPPED) (tests/\S+::.+?)|(tests/\S+::.+?) (PASSED|FAILED|SKIPPED))(?:\s+\[\s*\d+%\])?\s*$"
)

RUNS = "repos/{owner}/{repo}/actions"
DEV_PUSHES = f"{RUNS}/runs?branch=dev&event=push&status=completed&per_page=100"
RUNS_JQ = ".workflow_runs[] | {id, name, path, conclusion, created_at, html_url} | @json"
JOBS_JQ = ".jobs[] | {name, conclusion} | @json"


def test_results(log: str, job: str = "") -> list[dict]:
    tests = []
    for line in log.splitlines():
        match = TEST.search(line)
        if match:
            tests.append(
                {"job": job or line.split("\t")[0], "nodeid": match[2] or match[3], "outcome": match[1] or match[4]}
            )
    return tests


def _gh(args, run):
    endpoint = args[1]
    caller = "doctor.ci.job_log" if endpoint.endswith("/logs") else "doctor.ci"
    github_reads.charge(caller, endpoint, time.time())
    return run(["gh", *args], capture_output=True, text=True, check=True, timeout=60).stdout


def _pages(endpoint, field, run):
    output = _gh(["api", endpoint, "--paginate", "--jq", f".{field}[] | @json"], run)
    return [json.loads(line) for line in output.splitlines()]


def _belongs(run, pr):
    if run["head_sha"] != pr["head"]["sha"] or run["event"] not in {"pull_request", "pull_request_target"}:
        return False
    linked = run.get("pull_requests", [])
    return any(p["number"] == pr["number"] for p in linked) if linked else run["head_branch"] == pr["head"]["ref"]


def _attempts(repo, runs, run, cache=None):
    attempts = []
    for workflow in runs:
        latest = workflow["run_attempt"]
        if latest < 2:
            continue
        completed = latest if workflow["status"] == "completed" else latest - 1
        for number in range(1, completed + 1):
            attempts.append(_attempt(repo, workflow, number, run, cache))
    return attempts


def _job_tests(repo, job, run):
    endpoint = f"repos/{repo}/actions/jobs/{job['id']}/logs"
    try:
        log = github_reads.job_log(endpoint, lambda: _gh(["api", endpoint], run))
    except subprocess.CalledProcessError as exc:
        if "HTTP 404" not in exc.stderr:
            raise
        return []
    return test_results(log, job["name"])


def _attempt(repo, workflow, number, run, cache):
    key = f"{ROOT}:doctor:ci-attempt:{repo}:{workflow['id']}:{number}"
    saved = ci_cache.read(cache, key) if cache is not None else None
    if saved is not None:
        return saved
    endpoint = f"repos/{repo}/actions/runs/{workflow['id']}/attempts/{number}/jobs?per_page=100"
    tests = []
    for job in _pages(endpoint, "jobs", run):
        if job["status"] == "completed" and job["conclusion"] != "skipped":
            tests.extend(_job_tests(repo, job, run))
    attempt = {"run_id": workflow["id"], "attempt": number, "head_sha": workflow["head_sha"], "tests": tests}
    if cache is not None:
        ci_cache.write(cache, key, attempt)
    return attempt


def _effective_checks(checks: list[dict], runs: list[dict]) -> list[dict]:
    passed = {
        (r["workflow_id"], r["head_sha"])
        for r in runs
        if r["status"] == "completed" and r["conclusion"] == "success" and "workflow_id" in r
    }
    superseded = {
        r["check_suite_id"]
        for r in runs
        if r["conclusion"] == "cancelled" and (r.get("workflow_id"), r["head_sha"]) in passed and "check_suite_id" in r
    }
    return [c for c in checks if c.get("check_suite", {}).get("id") not in superseded]


def pull_request(repo: str, number: int, run: Callable = subprocess.run, cache: Redis | None = None) -> dict:
    root = f"repos/{repo}"
    pr = json.loads(_gh(["api", f"{root}/pulls/{number}"], run))
    sha = pr["head"]["sha"]
    checks = _pages(f"{root}/commits/{sha}/check-runs?per_page=100", "check_runs", run)
    workflows = _pages(f"{root}/actions/runs?head_sha={sha}&per_page=100", "workflow_runs", run)
    checks = _effective_checks(checks, workflows)
    runs = [r for r in workflows if _belongs(r, pr)]
    return {
        "pr": pr,
        "checks": checks,
        "runs": runs,
        "attempts": _attempts(repo, runs, run, cache),
    }


def _repo_rows(repo_dir, endpoint, jq, run):
    output = run(
        ["gh", "api", endpoint, "--jq", jq], cwd=repo_dir, capture_output=True, text=True, check=True, timeout=60
    ).stdout
    return [json.loads(line) for line in output.splitlines()]


def dev_pushes(repo_dir: str, run: Callable = subprocess.run) -> dict:
    runs = _repo_rows(repo_dir, DEV_PUSHES, RUNS_JQ, run)
    green, jobs = {}, {}
    for path, streak in ci.red_streaks(runs).items():
        last = _repo_rows(
            repo_dir,
            f"{RUNS}/workflows/{path.split('/')[-1]}/runs?branch=dev&event=push&status=success&per_page=1",
            RUNS_JQ,
            run,
        )
        if last:
            green[path] = last[0]
        found = _repo_rows(
            repo_dir, f"repos/{{owner}}/{{repo}}/actions/runs/{streak[0]['id']}/jobs?per_page=100", JOBS_JQ, run
        )
        jobs[streak[0]["id"]] = sorted(j["name"] for j in found if j["conclusion"] in ci.DEV_FAILED)
    return {"runs": runs, "green": green, "jobs": jobs}
