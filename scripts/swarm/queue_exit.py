import re
import subprocess
from datetime import UTC, datetime

from scripts.swarm import merge_queue
from scripts.swarm.store import SwarmError

PULL = re.compile(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)")
QUEUE_BRANCH = re.compile(r"^gh-readonly-queue/.+/pr-(\d+)-[0-9a-f]+$")
CLEAN = ("success", "cancelled", "skipped", "neutral")
PAGE = 100
PAGES = 5


def _own(item: dict, number: str) -> bool:
    match = QUEUE_BRANCH.match(item["head_branch"])
    return bool(match) and match.group(1) == number


def own_failures(url: str, since_ms: int, run=subprocess.run) -> list[str] | None:
    """Merge group runs on the pull request's own queue branch that failed since since_ms; None when not all listed."""
    repo, number = PULL.fullmatch(url).groups()
    since = datetime.fromtimestamp(since_ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    listed = []
    for page in range(1, PAGES + 1):
        path = f"repos/{repo}/actions/runs?event=merge_group&created=>={since}&per_page={PAGE}&page={page}"
        body = merge_queue.rest(path, run)
        listed += body["workflow_runs"]
        if len(listed) >= body["total_count"] or not body["workflow_runs"]:
            break
    if len(listed) < body["total_count"]:
        return None
    own = [item for item in listed if _own(item, number)]
    if running := [item["html_url"] for item in own if item["conclusion"] is None]:
        raise SwarmError(f"merge group runs still running: {', '.join(running)}")
    return [f"{item['name']} {item['html_url']}" for item in own if item["conclusion"] not in CLEAN]
