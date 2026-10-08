from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from typing import TYPE_CHECKING

from scripts.swarm.keyspace import ROOT

if TYPE_CHECKING:
    from redis import Redis

TEST = re.compile(
    r"(?:(PASSED|FAILED|SKIPPED) (tests/\S+::.+?)|(tests/\S+::.+?) (PASSED|FAILED|SKIPPED))(?:\s+\[\s*\d+%\])?\s*$"
)


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


def _attempt(repo, workflow, number, run, cache):
    key = f"{ROOT}:doctor:ci-attempt:{repo}:{workflow['id']}:{number}"
    saved = cache.get(key) if cache is not None else None
    if saved is not None:
        return json.loads(saved)
    endpoint = f"repos/{repo}/actions/runs/{workflow['id']}/attempts/{number}/jobs?per_page=100"
    tests = []
    for job in _pages(endpoint, "jobs", run):
        if job["status"] == "completed" and job["conclusion"] != "skipped":
            log = _gh(["api", f"repos/{repo}/actions/jobs/{job['id']}/logs"], run)
            tests.extend(test_results(log, job["name"]))
    attempt = {"run_id": workflow["id"], "attempt": number, "head_sha": workflow["head_sha"], "tests": tests}
    if cache is not None:
        cache.set(key, json.dumps(attempt), ex=604800)
    return attempt


def pull_request(repo: str, number: int, run: Callable = subprocess.run, cache: Redis | None = None) -> dict:
    root = f"repos/{repo}"
    pr = json.loads(_gh(["api", f"{root}/pulls/{number}"], run))
    sha = pr["head"]["sha"]
    checks = _pages(f"{root}/commits/{sha}/check-runs?per_page=100", "check_runs", run)
    runs = [
        r for r in _pages(f"{root}/actions/runs?head_sha={sha}&per_page=100", "workflow_runs", run) if _belongs(r, pr)
    ]
    return {
        "pr": pr,
        "checks": checks,
        "runs": runs,
        "attempts": _attempts(repo, runs, run, cache),
    }
