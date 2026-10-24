import re
import subprocess
from datetime import UTC, datetime

from scripts.swarm import merge_queue
from scripts.swarm.store import SwarmError

PULL = re.compile(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)")
QUEUE_BRANCH = re.compile(r"^gh-readonly-queue/.+/pr-(\d+)-[0-9a-f]+$")
CLEAN = ("success", "cancelled", "skipped", "neutral")
FAILED_JOB = ("failure", "timed_out", "startup_failure")
PAGE = 100
PAGES = 5


def _own(item: dict, number: str) -> bool:
    match = QUEUE_BRANCH.match(item["head_branch"])
    return bool(match) and match.group(1) == number


def _listed(repo: str, since: str, run) -> list[dict] | None:
    listed = []
    for page in range(1, PAGES + 1):
        path = f"repos/{repo}/actions/runs?event=merge_group&created=>={since}&per_page={PAGE}&page={page}"
        body = merge_queue.rest(path, run)
        listed += body["workflow_runs"]
        if len(listed) >= body["total_count"] or not body["workflow_runs"]:
            break
    return listed if len(listed) >= body["total_count"] else None


def _failed(repo: str, item: dict, run) -> bool:
    if item["conclusion"] not in CLEAN:
        return True
    if item["conclusion"] != "cancelled":
        return False
    jobs = []
    for page in range(1, PAGES + 1):
        body = merge_queue.rest(f"repos/{repo}/actions/runs/{item['id']}/jobs?per_page={PAGE}&page={page}", run)
        jobs += body["jobs"]
        if len(jobs) >= body["total_count"] or not body["jobs"]:
            break
    return len(jobs) < body["total_count"] or any(job["conclusion"] in FAILED_JOB for job in jobs)


def own_failures(url: str, since_ms: int, run=subprocess.run) -> list[str] | None:
    """Merge group runs on the pull request's own queue branch with a failed job since since_ms; None when not all listed."""
    repo, number = PULL.fullmatch(url).groups()
    since = datetime.fromtimestamp(since_ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    listed = _listed(repo, since, run)
    if listed is None:
        return None
    own = [item for item in listed if _own(item, number)]
    if running := [item["html_url"] for item in own if item["conclusion"] is None]:
        raise SwarmError(f"merge group runs still running: {', '.join(running)}")
    return [f"{item['name']} {item['html_url']}" for item in own if _failed(repo, item, run)]


def require_fresh(url: str, head: str, run=subprocess.run) -> None:
    repo, number = PULL.fullmatch(url).groups()
    since = merge_queue.rest(f"repos/{repo}/commits/{head}", run)["commit"]["committer"]["date"]
    listed = _listed(repo, since, run)
    if listed is None:
        raise SwarmError(f"could not list every merge group run since head {head} landed")
    own = [item for item in listed if _own(item, number)]
    if not own:
        return
    group = max(own, key=lambda item: item["created_at"])["head_sha"]
    last = [item for item in own if item["head_sha"] == group and item["conclusion"] is not None]
    if failed := [f"{item['name']} {item['html_url']}" for item in last if _failed(repo, item, run)]:
        raise SwarmError(
            f"head {head} failed its last merge group run ({', '.join(failed)}); push a new commit before queueing again"
        )
