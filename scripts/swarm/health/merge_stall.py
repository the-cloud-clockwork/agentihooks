import json
import subprocess
import time
from collections import Counter
from contextvars import ContextVar
from datetime import datetime

from scripts.swarm import github_reads, timing
from scripts.swarm.health.findings import Finding
from scripts.swarm.store import SwarmError

DEADLINE = ContextVar("merge_observation_deadline", default=None)
HOUR_MS = 3_600_000
CACHE_MS = 300_000
RUNS = 10
STREAK = 3
FAILED = frozenset({"failure", "timed_out", "action_required", "startup_failure"})
QUERY = """
query($owner:String!,$name:String!,$cursor:String){
  repository(owner:$owner,name:$name){
    mergeQueue(branch:"dev"){entries(first:1){totalCount}}
    pullRequests(first:100,after:$cursor,states:MERGED,baseRefName:"dev",
                 orderBy:{field:UPDATED_AT,direction:DESC}){
      nodes{mergedAt updatedAt} pageInfo{hasNextPage endCursor}
    }
  }
}
"""


def request(repo: str, args: list[str]) -> dict:
    timing.keep()
    remaining = 20 if DEADLINE.get() is None else DEADLINE.get() - time.monotonic()
    if remaining <= 0:
        raise SwarmError("GitHub merge observation exhausted its read budget")
    with timing.step("scripts.swarm.health.merge_stall.github_read"):
        result = github_reads.rest(
            "swarm.merge_stall",
            args,
            subprocess.run,
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=min(20, remaining),
        )
    if result.returncode:
        raise SwarmError(result.stderr.strip() or "GitHub API failed")
    data = json.loads(result.stdout)
    if data.get("errors"):
        raise SwarmError("; ".join(error["message"] for error in data["errors"]))
    return data


def timestamp(text: str) -> int:
    return int(datetime.fromisoformat(text).timestamp() * 1000)


def queue(repo: str, name: str) -> tuple[int, int]:
    owner, project = name.split("/")
    args = ["api", "graphql", "-f", f"query={QUERY}", "-f", f"owner={owner}", "-f", f"name={project}"]
    merged, cursor = 0, None
    while True:
        page_args = args + (["-f", f"cursor={cursor}"] if cursor else [])
        data = request(repo, page_args)["data"]["repository"]
        length = data["mergeQueue"]["entries"]["totalCount"] if data["mergeQueue"] else 0
        if not length:
            return 0, 0
        pulls = data["pullRequests"]
        nodes = pulls["nodes"]
        merged = max([merged, *(timestamp(node["mergedAt"]) for node in nodes)])
        if not pulls["pageInfo"]["hasNextPage"] or min(timestamp(node["updatedAt"]) for node in nodes) <= merged:
            return length, merged
        cursor = pulls["pageInfo"]["endCursor"]


def failed_checks(repo: str, name: str) -> str:
    runs = request(repo, ["api", f"repos/{name}/actions/runs?event=merge_group&per_page={RUNS}"])["workflow_runs"]
    counts = Counter()
    for run in runs:
        if run["conclusion"] in ("success", "skipped"):
            continue
        page, failed = 1, set()
        while True:
            jobs = request(repo, ["api", f"repos/{name}/actions/runs/{run['id']}/jobs?per_page=100&page={page}"])[
                "jobs"
            ]
            failed.update(job["name"] for job in jobs if job["conclusion"] in FAILED)
            if len(jobs) < 100:
                break
            page += 1
        counts.update(failed)
    if not counts:
        return "none observed"
    check = min(counts, key=lambda name: (-counts[name], name))
    return f"{check} ({counts[check]} runs)"


def observe(repo: str, at: int) -> dict:
    token = DEADLINE.set(time.monotonic() + 10)
    try:
        name = request(repo, ["repo", "view", "--json", "nameWithOwner"])["nameWithOwner"]
        length, merged = queue(repo, name)
        streak, head = failing(repo, name)
        stalled = length and merged and at - merged >= HOUR_MS
        failed = failed_checks(repo, name) if stalled else "none observed"
        return {
            "repo": name,
            "queue": length,
            "merged": merged,
            "failed": failed,
            "streak": streak,
            "head": head,
            "at": at,
        }
    finally:
        DEADLINE.reset(token)


def failing(repo: str, name: str) -> tuple[int, int]:
    path = f"repos/{name}/actions/runs?event=merge_group&status=completed&per_page={RUNS}"
    streak, head = 0, 0
    for run in request(repo, ["api", path])["workflow_runs"]:
        if run["conclusion"] in ("cancelled", "skipped"):
            continue
        if run["conclusion"] not in FAILED:
            break
        streak, head = streak + 1, head or run["id"]
    return streak, head


def findings(store, slug: str, repo: str, at: int) -> list[Finding]:
    key = store.key(slug, "merge-stall")
    state = json.loads(store.redis.get(key) or "{}")
    if not state or at - state["tried"] >= CACHE_MS:
        try:
            observed = observe(repo, at)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, SwarmError) as exc:
            state.update(error=str(exc))
        else:
            if observed["merged"] != state.get("merged") or not observed["queue"]:
                store.redis.delete(store.key(slug, "merge-stall-notice"))
            state = observed
        state["tried"] = at
        store.redis.set(key, json.dumps(state))
    return _stalled(state) + _failing(state)


def _failing(state: dict) -> list[Finding]:
    streak = state.get("streak", 0)
    if streak < STREAK:
        return []
    return [
        Finding(
            "merge failures",
            state["repo"],
            f"The last {'at least ' if streak >= RUNS else ''}{streak} merge group runs failed in a row "
            f"with {state['queue']} queued entries",
            (f"Queue length: {state['queue']}", f"Newest failed merge group run: {state['head']}"),
            f"{STREAK} merge group runs failing in a row",
            streak,
        )
    ]


def _stalled(state: dict) -> list[Finding]:
    if not state.get("queue") or not state.get("merged") or state["at"] - state["merged"] < HOUR_MS:
        return []
    minutes = (state["at"] - state["merged"]) // 60_000
    return [
        Finding(
            "merge stall",
            state["repo"],
            f"Dev has had no merge for {minutes} minutes with {state['queue']} queued entries",
            (
                f"Queue length: {state['queue']}",
                f"Time since last dev merge: {minutes} minutes",
                f"Most common failed check in the last {RUNS} queue runs: {state['failed']}",
            ),
            "sixty minutes without a dev merge while the queue holds entries; observation only",
            minutes,
        )
    ]


def deliver(inbox, store, slug: str) -> list[str]:
    state = json.loads(store.redis.get(store.key(slug, "merge-stall")) or "{}")
    if not state.get("queue") or state.get("error") or not state.get("merged"):
        return []
    if state["at"] - state["merged"] < HOUR_MS:
        return []
    key = store.key(slug, "merge-stall-notice")
    sent = store.redis.get(key)
    if sent is not None and state["at"] - int(sent) < HOUR_MS:
        return []
    found = findings(store, slug, "", state["at"])[0]
    address = f"master@{slug}"
    text = f"Merge stall on {state['repo']}: {found.summary}. " + "; ".join(found.evidence)
    inbox.send("swarm", address, text, ref=f"merge-stall:{slug}:{state['merged']}:{state['at']}")
    store.redis.set(key, state["at"])
    return [f"told {address}: {text}"]
