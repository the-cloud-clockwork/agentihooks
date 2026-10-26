"""The merge path of one repository over a window: Tests runs, merge queue entries and removals, and pull requests."""

import argparse
import json
import re
import statistics
import subprocess
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from scripts.swarm import ci_speed
from scripts.swarm.store import SwarmError

EVENTS = ("pull_request", "merge_group", "push")
PAGE = 100
LISTED_CAP = 1000
STANDARD = frozenset(("anton-oss", "anton-oss-queue"))
SHORT = {"pull_request": "head", "merge_group": "queue"}
WORDS = 50
QUEUE_EVENTS = "ADDED_TO_MERGE_QUEUE_EVENT, REMOVED_FROM_MERGE_QUEUE_EVENT"
TIMELINE = 100
PULLS = (
    "query($q: String!, $endCursor: String) { search(query: $q, type: ISSUE, first: 50, after: $endCursor) {"
    " issueCount pageInfo { hasNextPage endCursor } nodes { ... on PullRequest { url createdAt mergedAt"
    f" timelineItems(last: {TIMELINE}, itemTypes: [{QUEUE_EVENTS}]) {{ totalCount nodes {{ __typename"
    " ... on AddedToMergeQueueEvent { createdAt } ... on RemovedFromMergeQueueEvent { createdAt reason } } } } } } }"
)
OPEN_READY = "query($q: String!) { search(query: $q, type: ISSUE, first: 1) { issueCount } }"
DEV_MERGE = re.compile(r"Merge (remote-tracking )?branch '(origin/)?dev'")
AFTER = {"failure": "after red head", "success": "review fix"}
LOOKBACK_S = 6 * 3600
SERVER_ERROR = re.compile(r"HTTP 5\d\d|stream error")
ATTEMPTS = 5
RETRY_S = 10
JOB_PAGE = 30
MATRIX = re.compile(r" \(.*\)$")


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


def _promotion(run: dict) -> bool:
    """Pull request runs from dev or a promotion snapshot grade promotions into main, not the merge path into dev."""
    return run["event"] == "pull_request" and (
        run["head_branch"] == "dev" or run["head_branch"].startswith("promotion/")
    )


def _queue_events(pulls: list[dict], since_s: float, until_s: float) -> tuple[int, Counter]:
    events = [
        event
        for pull in pulls
        for event in pull["timelineItems"]["nodes"]
        if _within(event.get("createdAt"), since_s, until_s)
    ]
    added = sum(event["__typename"] == "AddedToMergeQueueEvent" for event in events)
    removed = Counter(
        event["reason"] or "unknown" for event in events if event["__typename"] == "RemovedFromMergeQueueEvent"
    )
    return added, removed


def count(runs: list[dict], pulls: list[dict], jobs: dict, open_ready: int, window: tuple[float, float]) -> dict:
    """Counts the merge path over [since, until); jobs maps a merge group run id to its job runner labels."""
    since_s, until_s = window
    merged = [pull for pull in pulls if _within(pull.get("mergedAt"), since_s, until_s)]
    mine = [run for run in runs if _within(run["created_at"], since_s, until_s) and not _promotion(run)]
    by_event = Counter(run["event"] for run in mine)
    heads = [run for run in mine if run["event"] == "pull_request" and run["conclusion"] in ci_speed.FINISHED]
    queue = [run for run in mine if run["event"] == "merge_group"]
    cycles = [
        _seconds(run["updated_at"]) - _seconds(run["run_started_at"])
        for run in queue
        if run["status"] == "completed" and run["conclusion"] in ci_speed.FINISHED
    ]
    standard = [
        sum(bool(STANDARD & set(labels)) for labels in jobs[run["id"]])
        for run in queue
        if run["id"] in jobs and run["status"] == "completed"
    ]
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

    def removed(keep):
        named = [f"{n} {why.lower().replace('_', ' ')}" for why, n in causes[:keep]]
        rest = sum(n for _, n in causes[keep:])
        return ", ".join(named + ([f"{rest} other"] if rest else [])) or "none"

    runs = ", ".join(f"{n} {SHORT.get(event, event)}" for event, n in found["tests_runs"].items()) or "none"
    causes = sorted(
        ((why, n) for why, n in found["removals"].items() if why.lower() != "merged"), key=lambda cause: -cause[1]
    )
    rate = found["head_failure_rate"]
    for keep in range(len(causes), -1, -1):
        line = (
            f"{repo.rpartition('/')[2].replace('-', ' ')}, {found['hours']} hours: {found['merges']} merges, "
            f"{found['merges_per_hour']} an hour. Runs {runs}. {found['cancelled_pull_request_runs']} cancelled, "
            f"{found['reruns']} reruns. {found['enqueues']} enqueues, removed {removed(keep)}. "
            f"Heads fail {shown(None if rate is None else round(rate * 100))} percent, "
            f"{found['failed_queue_runs']} queue runs failed. Queue cycle {shown(found['queue_cycle_minutes'])} "
            f"minutes, {shown(found['standard_jobs_per_queue_run'])} standard jobs. "
            f"{found['open_ready_pull_requests']} open ready. Lead {shown(found['lead_time_minutes'])} minutes."
        )
        if len(line.split()) <= WORDS:
            break
    return line


def _gh(args: list[str], repo_dir: str, run: Callable) -> str:
    left = ATTEMPTS
    while True:
        left -= 1
        try:
            done = run(["gh", *args], cwd=repo_dir, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            raise SwarmError(f"GitHub API: {exc}") from exc
        if not done.returncode:
            return done.stdout
        if not left or not SERVER_ERROR.search(done.stderr):
            raise SwarmError(done.stderr.strip() or "GitHub API failed")
        time.sleep(RETRY_S * (ATTEMPTS - left))


def read_runs(repo_dir: str, event: str, window: tuple[float, float], run: Callable) -> list[dict]:
    created = f"{_stamp(window[0])}..{_stamp(window[1])}"
    listed = []
    for page in range(1, LISTED_CAP // PAGE + 1):
        path = (
            f"repos/{{owner}}/{{repo}}/actions/workflows/{ci_speed.WORKFLOW}/runs?event={event}"
            f"{'&branch=dev' if event == 'push' else ''}&created={created}"
            f"&exclude_pull_requests=true&per_page={PAGE}&page={page}"
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
    path = f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}/jobs?per_page={JOB_PAGE}"
    out = _gh(["api", path, "--paginate", "--jq", ".jobs[] | .labels | @json"], repo_dir, run)
    return [json.loads(line) for line in out.splitlines()]


def _search(repo_dir: str, query: str, variables: dict, run: Callable, paginate: bool = False) -> list[dict]:
    args = [
        "api",
        "graphql",
        *(["--paginate"] if paginate else []),
        "--jq",
        ".data.search | @json",
        "-f",
        f"query={query}",
    ]
    for name, value in variables.items():
        args += ["-f", f"{name}={value}"]
    out = _gh(args, repo_dir, run)
    try:
        pages = [json.loads(line) for line in out.splitlines()]
    except ValueError as exc:
        raise SwarmError(f"GitHub search answered unreadably: {exc}") from exc
    if not pages:
        raise SwarmError("GitHub search answered nothing")
    if not all(isinstance(page, dict) and "issueCount" in page for page in pages):
        raise SwarmError("GitHub search answered without a result count")
    return pages


def read_pulls(repo_dir: str, repo: str, since_s: float, run: Callable) -> list[dict]:
    pages = _search(repo_dir, PULLS, {"q": f"repo:{repo} is:pr base:dev updated:>={_stamp(since_s)}"}, run, True)
    pulls = [node for page in pages for node in page["nodes"] if node]
    total = pages[0]["issueCount"]
    if len(pulls) < total:
        raise SwarmError(f"listed {len(pulls)} of {total} updated pull requests; shorten the window")
    if cut := [pull["url"] for pull in pulls if pull["timelineItems"]["totalCount"] > TIMELINE]:
        raise SwarmError(f"more than {TIMELINE} merge queue events on {', '.join(cut)}; shorten the window")
    return pulls


def collect(
    repo_dir: str, window: tuple[float, float], run: Callable = subprocess.run, queue_jobs: bool = True
) -> tuple[str, dict]:
    repo = _gh(["api", "repos/{owner}/{repo}", "--jq", ".full_name"], repo_dir, run).strip()
    runs = [found for event in EVENTS for found in read_runs(repo_dir, event, window, run)]
    jobs, unread = {}, 0
    for found in runs:
        if not queue_jobs or found["event"] != "merge_group":
            continue
        try:
            jobs[found["id"]] = read_jobs(repo_dir, found["id"], run)
        except SwarmError:
            unread += 1
    pulls = read_pulls(repo_dir, repo, window[0], run)
    open_pages = _search(repo_dir, OPEN_READY, {"q": f"repo:{repo} is:pr is:open draft:false base:dev"}, run)
    ready = open_pages[0]["issueCount"]
    return repo, {**count(runs, pulls, jobs, ready, window), "queue_runs_without_jobs": unread}


def _cause(run: dict, before: dict | None) -> str:
    if DEV_MERGE.match(str((run.get("head_commit") or {}).get("message"))):
        return "dev merge"
    if before is None:
        return "first push"
    if before["head_sha"] == run["head_sha"]:
        return "same head"
    return AFTER.get(before["conclusion"], "after superseded head")


def head_pushes(runs: list[dict], window: tuple[float, float]) -> dict:
    """Classes each pull request head Tests run created in the window by the head run before it on its branch."""
    heads = sorted(
        (found for found in runs if found["event"] == "pull_request" and not _promotion(found)),
        key=lambda found: (found["created_at"], found["id"]),
    )
    last, causes = {}, {}
    for found in heads:
        cause = _cause(found, last.get(found["head_branch"]))
        last[found["head_branch"]] = found
        if _within(found["created_at"], *window):
            tally = causes.setdefault(cause, {"runs": 0, "red": 0})
            tally["runs"] += 1
            tally["red"] += found["conclusion"] == "failure"
    return dict(sorted(causes.items(), key=lambda item: -item[1]["runs"]))


def red_head_jobs(runs: list[dict], window: tuple[float, float], repo_dir: str, run: Callable) -> dict:
    """Counts red pull request head Tests runs created in the window by each job that failed them."""
    failed = Counter()
    for found in runs:
        if found["event"] != "pull_request" or _promotion(found) or found["conclusion"] != "failure":
            continue
        if not _within(found["created_at"], *window):
            continue
        path = f"repos/{{owner}}/{{repo}}/actions/runs/{found['id']}/jobs?filter=latest&per_page={PAGE}"
        query = '.jobs[] | select(.conclusion == "failure") | .name'
        try:
            names = _gh(["api", path, "--paginate", "--jq", query], repo_dir, run).splitlines()
        except SwarmError:
            names = ["unreadable"]
        failed.update({MATRIX.sub("", name) for name in names} or {"no failed job"})
    return dict(failed.most_common())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--until")
    parser.add_argument("--out", type=Path, default=Path("ci-minutes.json"))
    args = parser.parse_args(argv)
    until_s = _seconds(args.until) if args.until else time.time()
    window = (until_s - args.hours * 3600, until_s)
    repo, found = collect(".", window, queue_jobs=False)
    history = read_runs(".", "pull_request", (window[0] - LOOKBACK_S, until_s), subprocess.run)
    found["head_pushes"] = head_pushes(history, window)
    found["red_head_jobs"] = red_head_jobs(history, window, ".", subprocess.run)
    args.out.write_text(json.dumps({"repo": repo, **found}))
    print(render(repo, found))
    print(json.dumps(found["head_pushes"]))
    print(json.dumps(found["red_head_jobs"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
