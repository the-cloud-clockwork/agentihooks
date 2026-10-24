"""The merge path of one repository over a window: Tests runs, merge queue entries and removals, and pull requests."""

import json
import statistics
import subprocess
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime

from scripts.swarm import ci_speed
from scripts.swarm.store import SwarmError

EVENTS = ("pull_request", "merge_group", "push")
PAGE = 100
LISTED_CAP = 1000
STANDARD = frozenset(("anton-oss", "anton-oss-queue"))
SHORT = {"pull_request": "head", "merge_group": "queue"}
QUEUE_EVENTS = "ADDED_TO_MERGE_QUEUE_EVENT, REMOVED_FROM_MERGE_QUEUE_EVENT"
PULLS = (
    "query($q: String!, $endCursor: String) { search(query: $q, type: ISSUE, first: 50, after: $endCursor) {"
    " pageInfo { hasNextPage endCursor } nodes { ... on PullRequest { url createdAt mergedAt"
    f" timelineItems(last: 100, itemTypes: [{QUEUE_EVENTS}]) {{ nodes {{ __typename"
    " ... on AddedToMergeQueueEvent { createdAt } ... on RemovedFromMergeQueueEvent { createdAt reason } } } } } } }"
)
OPEN_READY = "query($q: String!) { search(query: $q, type: ISSUE, first: 1) { issueCount } }"


def _seconds(stamp: str) -> float:
    return datetime.fromisoformat(stamp).timestamp()


def _stamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _within(stamp: str | None, since_s: float, until_s: float) -> bool:
    return stamp is not None and since_s <= _seconds(stamp) < until_s


def _median_minutes(seconds: list[float]) -> float | None:
    return round(statistics.median(seconds) / 60, 1) if seconds else None


def _per(count: int, total: int) -> float | None:
    return round(count / total, 2) if total else None


def _queue_events(pulls: list[dict], since_s: float, until_s: float) -> tuple[int, Counter]:
    events = [
        event
        for pull in pulls
        for event in pull["timelineItems"]["nodes"]
        if _within(event.get("createdAt"), since_s, until_s)
    ]
    added = sum(event["__typename"] == "AddedToMergeQueueEvent" for event in events)
    removed = Counter(event["reason"] for event in events if event["__typename"] == "RemovedFromMergeQueueEvent")
    return added, removed


def count(runs: list[dict], pulls: list[dict], jobs: dict, open_ready: int, window: tuple[float, float]) -> dict:
    """Counts the merge path over [since, until); jobs maps a merge group run id to its job runner labels."""
    since_s, until_s = window
    merged = [pull for pull in pulls if _within(pull.get("mergedAt"), since_s, until_s)]
    mine = [run for run in runs if _within(run["created_at"], since_s, until_s)]
    by_event = Counter(run["event"] for run in mine)
    heads = [run for run in mine if run["event"] == "pull_request" and run["conclusion"] in ci_speed.FINISHED]
    queue = [run for run in mine if run["event"] == "merge_group"]
    cycles = [
        _seconds(run["updated_at"]) - _seconds(run["run_started_at"])
        for run in queue
        if run["status"] == "completed" and run["conclusion"] in ci_speed.FINISHED
    ]
    standard = [sum(bool(STANDARD & set(labels)) for labels in jobs[run["id"]]) for run in queue if run["id"] in jobs]
    enqueues, removals = _queue_events(pulls, since_s, until_s)
    return {
        "hours": round((until_s - since_s) / 3600, 2),
        "merges": len(merged),
        "merges_per_hour": round(len(merged) * 3600 / (until_s - since_s), 2),
        "tests_runs": dict(sorted(by_event.items())),
        "tests_runs_per_merge": {event: _per(n, len(merged)) for event, n in sorted(by_event.items())},
        "cancelled_pull_request_runs": sum(
            run["event"] == "pull_request" and run["conclusion"] == "cancelled" for run in mine
        ),
        "reruns": sum(run["run_attempt"] > 1 for run in mine),
        "enqueues": enqueues,
        "removals": dict(sorted(removals.items())),
        "head_failure_rate": _per(sum(run["conclusion"] == "failure" for run in heads), len(heads)),
        "failed_queue_runs": sum(run["conclusion"] == "failure" for run in queue),
        "queue_cycle_minutes": _median_minutes(cycles),
        "standard_jobs_per_queue_run": round(statistics.mean(standard), 1) if standard else None,
        "open_ready_pull_requests": open_ready,
        "lead_time_minutes": _median_minutes(
            [_seconds(pull["mergedAt"]) - _seconds(pull["createdAt"]) for pull in merged]
        ),
    }


def render(repo: str, found: dict) -> str:
    """One plain line for a ledger comment: words and numbers only, no ids or dashes, within fifty words."""

    def shown(value):
        return "unknown" if value is None else value

    runs = ", ".join(f"{n} {SHORT.get(event, event)}" for event, n in found["tests_runs"].items()) or "none"
    causes = {why: n for why, n in found["removals"].items() if why != "MERGED"}
    removed = ", ".join(f"{n} {why.lower().replace('_', ' ')}" for why, n in causes.items()) or "none"
    rate = found["head_failure_rate"]
    return (
        f"{repo.rpartition('/')[2]}, {found['hours']} hours: {found['merges']} merges, "
        f"{found['merges_per_hour']} an hour. Runs {runs}. {found['cancelled_pull_request_runs']} cancelled, "
        f"{found['reruns']} reruns. {found['enqueues']} enqueues, removed {removed}. "
        f"Heads fail {shown(None if rate is None else round(rate * 100))} percent, "
        f"{found['failed_queue_runs']} queue runs failed. Queue cycle {shown(found['queue_cycle_minutes'])} minutes, "
        f"{shown(found['standard_jobs_per_queue_run'])} standard jobs. {found['open_ready_pull_requests']} open ready. "
        f"Lead {shown(found['lead_time_minutes'])} minutes."
    )


def _gh(args: list[str], repo_dir: str, run: Callable) -> str:
    try:
        done = run(["gh", *args], cwd=repo_dir, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SwarmError(f"GitHub API: {exc}") from exc
    if done.returncode:
        raise SwarmError(done.stderr.strip() or "GitHub API failed")
    return done.stdout


def read_runs(repo_dir: str, event: str, window: tuple[float, float], run: Callable) -> list[dict]:
    created = f"{_stamp(window[0])}..{_stamp(window[1])}"
    listed, total = [], 0
    for page in range(1, LISTED_CAP // PAGE + 1):
        path = (
            f"repos/{{owner}}/{{repo}}/actions/workflows/{ci_speed.WORKFLOW}/runs?event={event}"
            f"&created={created}&exclude_pull_requests=true&per_page={PAGE}&page={page}"
        )
        body = json.loads(_gh(["api", path], repo_dir, run))
        listed += body["workflow_runs"]
        total = body["total_count"]
        if len(listed) >= total or not body["workflow_runs"]:
            break
    if len(listed) < total:
        raise SwarmError(f"listed {len(listed)} of {total} {event} Tests runs; shorten the window")
    return listed


def read_jobs(repo_dir: str, run_id: int, run: Callable) -> list[list[str]]:
    path = f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}/jobs?per_page={PAGE}"
    out = _gh(["api", path, "--paginate", "--jq", ".jobs[] | .labels | @json"], repo_dir, run)
    return [json.loads(line) for line in out.splitlines()]


def _search(repo_dir: str, query: str, variables: dict, run: Callable, paginate: bool = False) -> list[dict]:
    args = ["api", "graphql", *(["--paginate"] if paginate else []), "-f", f"query={query}"]
    for name, value in variables.items():
        args += ["-f", f"{name}={value}"]
    out = _gh(args, repo_dir, run)
    decoder, pages, at = json.JSONDecoder(), [], 0
    while at < len(out.strip()):
        page, at = decoder.raw_decode(out, at)
        pages.append(page["data"]["search"])
        while at < len(out) and out[at].isspace():
            at += 1
    return pages


def collect(repo_dir: str, window: tuple[float, float], run: Callable = subprocess.run) -> tuple[str, dict]:
    repo = _gh(["api", "repos/{owner}/{repo}", "--jq", ".full_name"], repo_dir, run).strip()
    runs = [found for event in EVENTS for found in read_runs(repo_dir, event, window, run)]
    jobs = {found["id"]: read_jobs(repo_dir, found["id"], run) for found in runs if found["event"] == "merge_group"}
    updated = f"repo:{repo} is:pr updated:>={_stamp(window[0])}"
    pulls = [node for page in _search(repo_dir, PULLS, {"q": updated}, run, True) for node in page["nodes"] if node]
    ready = _search(repo_dir, OPEN_READY, {"q": f"repo:{repo} is:pr is:open draft:false"}, run)[0]["issueCount"]
    return repo, count(runs, pulls, jobs, ready, window)
