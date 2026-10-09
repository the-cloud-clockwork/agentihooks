import json
import re
import subprocess
from fnmatch import fnmatch

from scripts.swarm.store import SwarmError

FRESHNESS = """query($url: URI!) { resource(url: $url) { ... on PullRequest {
    number repository { nameWithOwner ref(qualifiedName: "refs/heads/dev") { target { oid } } }
} } }"""
GRADING = (
    ".github/*",
    ".semgrep/*",
    "tests/*",
    "scripts/ci*",
    "scripts/gate*",
    "scripts/check*",
    "scripts/size_limits.py",
    "mutation*",
    "delivery.yaml",
    "pyproject.toml",
    "uv.lock",
    "ruff.toml",
    ".ruff.toml",
    "sonar-project.properties",
    ".coveragerc",
)
FIELDS = "id state headRefOid baseRefName mergeQueueEntry { id position state }"
STATE = "query($url: URI!) { resource(url: $url) { ... on PullRequest { " + FIELDS + " } } }"
QUEUE = """mutation($id: ID!, $head: GitObjectID!) {
    enqueuePullRequest(input: {pullRequestId: $id, expectedHeadOid: $head}) { mergeQueueEntry { id } }
}"""

DEQUEUE = """mutation($id: ID!) {
    dequeuePullRequest(input: {id: $id}) { mergeQueueEntry { id } }
}"""


def graphql(query: str, variables: dict, run=subprocess.run) -> dict:
    command = ["gh", "api", "graphql", "-f", f"query={query}"]
    for name, value in variables.items():
        command += ["-f", f"{name}={value}"]
    try:
        done = run(command, capture_output=True, text=True, timeout=20)
        if done.returncode:
            raise SwarmError(done.stderr.strip() or "GitHub API failed")
        response = json.loads(done.stdout)
        if response.get("errors"):
            raise SwarmError("; ".join(error["message"] for error in response["errors"]))
        data = response["data"]
        if not isinstance(data, dict):
            raise SwarmError("GitHub API returned no data")
        return data
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        raise SwarmError(f"GitHub API: {exc}") from exc


def rest(path: str, run, fields: dict | None = None) -> dict | str:
    command = ["gh", "api", path]
    if fields is not None:
        command += ["--method", "PUT"]
        for name, value in fields.items():
            command += ["-f", f"{name}={value}"]
    try:
        done = run(command, capture_output=True, text=True, timeout=20)
        if done.returncode:
            raise SwarmError(done.stderr.strip() or "GitHub API failed")
        return done.stdout if path.endswith("/logs") else json.loads(done.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise SwarmError(f"GitHub API: {exc}") from exc


def checked_base(repo: str, head: str, run) -> str:
    runs = rest(f"repos/{repo}/actions/workflows/test.yml/runs?event=pull_request&head_sha={head}&per_page=1", run)
    records = runs["workflow_runs"]
    if not records or records[0]["conclusion"] != "success" or records[0]["head_sha"] != head:
        raise SwarmError("the current pull request head must pass Tests before queueing")
    artifacts = rest(f"repos/{repo}/actions/runs/{records[0]['id']}/artifacts?per_page=100", run)["artifacts"]
    named = {match[1] for item in artifacts if (match := re.fullmatch(r"checked-base-([0-9a-f]{40})", item["name"]))}
    if len(named) > 1:
        raise SwarmError("cannot establish the base of the green checks")
    if named:
        return named.pop()
    jobs = rest(f"repos/{repo}/actions/runs/{records[0]['id']}/jobs?per_page=100", run)["jobs"]
    job = next((job for job in jobs if job["name"] == "test-count (3.11)" and job["conclusion"] == "success"), None)
    if job is None:
        raise SwarmError("cannot establish the base of the green checks")
    log = rest(f"repos/{repo}/actions/jobs/{job['id']}/logs", run)
    bases = set(re.findall(r"^\S+\s+BASE: ([0-9a-f]{40})\s*$", log, re.MULTILINE))
    if len(bases) != 1:
        raise SwarmError("cannot establish the base of the green checks")
    return bases.pop()


def refresh(url: str, raw: dict, run) -> bool:
    pull = graphql(FRESHNESS, {"url": url}, run)["resource"]
    repo = pull["repository"]["nameWithOwner"]
    base = checked_base(repo, raw["headRefOid"], run)
    current = graphql(FRESHNESS, {"url": url}, run)["resource"]["repository"]["ref"]["target"]["oid"]
    if base == current:
        return False
    files = rest(f"repos/{repo}/compare/{base}...{current}", run)["files"]
    changed = len(files) >= 300 or any(
        fnmatch(file.get(key, ""), pattern)
        for file in files
        for key in ("filename", "previous_filename")
        for pattern in GRADING
    )
    latest = graphql(FRESHNESS, {"url": url}, run)["resource"]["repository"]["ref"]["target"]["oid"]
    if latest != current:
        raise SwarmError("dev advanced during the grading comparison; retry queueing")
    if changed:
        rest(f"repos/{repo}/pulls/{pull['number']}/update-branch", run, {"expected_head_sha": raw["headRefOid"]})
    return changed


def operate(action: str, url: str, run=subprocess.run) -> dict:
    raw = graphql(STATE, {"url": url}, run)["resource"]
    if not raw:
        raise SwarmError("GitHub URL does not identify a pull request")
    if action != "state" and raw["baseRefName"] != "dev":
        raise SwarmError("swarm merge queue operations require a pull request into dev")
    raw_head = raw["headRefOid"]
    waiting = False
    if action == "queue" and raw["mergeQueueEntry"] is None:
        waiting = refresh(url, raw, run)
        if not waiting:
            graphql(QUEUE, {"id": raw["id"], "head": raw["headRefOid"]}, run)
        raw = graphql(STATE, {"url": url}, run)["resource"]
    if action == "dequeue":
        graphql(DEQUEUE, {"id": raw["id"]}, run)
        raw = graphql(STATE, {"url": url}, run)["resource"]
    return {
        "url": url,
        "state": raw["state"],
        "head": raw["headRefOid"],
        "queued": raw["mergeQueueEntry"] is not None,
        "entry": raw["mergeQueueEntry"],
        **({"waiting": "checks", "previous_head": raw_head} if waiting else {}),
    }
