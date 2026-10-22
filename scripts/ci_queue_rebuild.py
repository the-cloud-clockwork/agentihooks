import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from scripts.ci_queue_retry import QUEUE_BRANCH
from scripts.swarm import merge_queue, queue_record
from scripts.swarm.store import SwarmError

QUERY = """query($url: URI!) { resource(url: $url) { ... on PullRequest {
  timelineItems(last: 20, itemTypes: [ADDED_TO_MERGE_QUEUE_EVENT, REMOVED_FROM_MERGE_QUEUE_EVENT]) { nodes {
    __typename
    ... on AddedToMergeQueueEvent { id createdAt actor { login } }
    ... on RemovedFromMergeQueueEvent { id createdAt actor { login } reason }
  } }
} } }"""
UNSEEN = (
    "no entry ahead was enqueued and this pull request was not removed while the group ran; "
    "a removal ahead with no queue run at the old parent, a base push or a ruleset change "
    "is not visible in queue entries"
)
CORRELATED = (
    "the queue entry was enqueued ahead of this pull request while the cancelled group ran; "
    "no queue event names the rebuild it caused"
)


def actor(event: dict) -> str | None:
    return (event.get("actor") or {}).get("login")


def window(events: list[dict], started: str, until: str) -> list[dict]:
    unique = {e["id"]: e for e in events if started <= e["createdAt"] <= until}
    return sorted(unique.values(), key=lambda e: e["createdAt"])


def timeline(url: str, run) -> list[dict]:
    return merge_queue.graphql(QUERY, {"url": url}, run)["resource"]["timelineItems"]["nodes"]


def base_pull(repo: str, parent: str | None, run) -> str | None:
    runs = merge_queue.rest(f"repos/{repo}/actions/runs?event=merge_group&head_sha={parent}", run)["workflow_runs"]
    found = next((m for r in runs if (m := QUEUE_BRANCH.match(r["head_branch"]))), None)
    return found and f"https://github.com/{repo}/pull/{found[1]}"


def base_removals(repo: str, parent: str | None, run) -> dict | None:
    try:
        ahead = base_pull(repo, parent, run)
        return ahead and {"pull": ahead, "events": timeline(ahead, run)}
    except (SwarmError, KeyError, TypeError) as exc:
        return {"gap": f"queue unreadable: {exc}"}


def cause(
    pull: str, started: str, until: str, groups: list[dict], events: list[dict], base: dict | None = None
) -> dict:
    events = window(events, started, until)
    removals = [e for e in events if e["__typename"] == "RemovedFromMergeQueueEvent"]
    adds = [e for e in events if e["__typename"] == "AddedToMergeQueueEvent"]
    own = next((g for g in groups if g["pull"] == pull), None)
    if own is None:
        if not removals:
            return {"action": "unknown", "gap": "the pull request left the queue with no removal event"}
        last = removals[-1]
        return {
            "action": "dequeue",
            "actor": actor(last),
            "reason": last.get("reason"),
            "at": last["createdAt"],
            "events": [last["id"]],
        }
    if removals and adds and adds[-1]["createdAt"] >= removals[-1]["createdAt"]:
        return {
            "action": "requeue",
            "actor": actor(adds[-1]),
            "removed_reason": removals[-1].get("reason"),
            "at": adds[-1]["createdAt"],
            "events": [removals[-1]["id"], adds[-1]["id"]],
        }
    if base and "gap" in base:
        return {"action": "unknown", "gap": base["gap"]}
    if base and (
        gone := [e for e in window(base["events"], started, until) if e["__typename"] == "RemovedFromMergeQueueEvent"]
    ):
        return {
            "action": "removed_ahead",
            "pull": base["pull"],
            "actor": actor(gone[-1]),
            "reason": gone[-1].get("reason"),
            "at": gone[-1]["createdAt"],
            "events": [gone[-1]["id"]],
        }
    ahead = [g for g in groups if g["position"] < own["position"] and started <= g["enqueued_at"] <= until]
    if not ahead:
        return {"action": "unknown", "gap": UNSEEN}
    return {
        "action": "unknown",
        "gap": CORRELATED,
        "correlated": [{key: g[key] for key in ("pull", "enqueuer", "enqueued_at", "jump")} for g in ahead],
    }


def record(observed: str, env, run, now: str | None = None) -> dict:
    now = now or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    repo, ref = env.get("GITHUB_REPOSITORY"), env.get("GITHUB_REF")
    number = ref and re.search(r"/pr-(\d+)-", ref)
    pull = f"https://github.com/{repo}/pull/{number[1]}" if repo and number else None
    started, old_parent, groups = None, None, []
    try:
        if pull is None:
            raise SwarmError("the queue ref names no pull request")
        started = merge_queue.rest(f"repos/{repo}/actions/runs/{env['GITHUB_RUN_ID']}", run)["created_at"]
        parents = merge_queue.rest(f"repos/{repo}/git/commits/{env['GITHUB_SHA']}", run)["parents"]
        old_parent = parents[0]["sha"] if parents else None
        journal = queue_record.Journal(pull, run)
        groups = journal.groups() or []
        if journal.gaps:
            raise SwarmError(journal.gaps[0])
        linked = cause(pull, started, now, groups, timeline(pull, run), base_removals(repo, old_parent, run))
    except (SwarmError, KeyError, TypeError) as exc:
        linked = {"action": "unknown", "gap": f"queue unreadable: {exc}"}
    own = next((g for g in groups if g["pull"] == pull), {})
    return {
        "id": f"queue-rebuild-{env.get('GITHUB_RUN_ID')}-{env.get('GITHUB_RUN_ATTEMPT')}",
        "at": now,
        "ref": ref,
        "pull": pull,
        "expected_head": own.get("pull_head"),
        "old_head": env.get("GITHUB_SHA"),
        "old_parent": old_parent,
        "observed_ref_head": observed or None,
        "new_head": own.get("head"),
        "new_base": own.get("base"),
        "started": started,
        "cause": linked,
        "groups": groups,
    }


def summary(found: dict) -> str:
    linked = found["cause"]
    action = linked["action"]
    if action == "dequeue":
        text = f"dequeue by {linked['actor']} ({linked['reason']})"
    elif action == "requeue":
        text = f"requeue by {linked['actor']}"
    elif action == "removed_ahead":
        text = f"removal of {linked['pull']} ahead by {linked['actor']} ({linked['reason']})"
    else:
        text = f"unknown cause: {linked['gap']}"
        for g in linked.get("correlated", []):
            text += f"; {'jump' if g['jump'] else 'enqueue'} of {g['pull']} by {g['enqueuer']} at {g['enqueued_at']}"
    return " ".join(f"record {found['id']}: {text}".split())


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    found = record(args[0] if args else "", os.environ, subprocess.run)
    print(f"queue-rebuild {json.dumps(found)}", file=sys.stderr)
    print(summary(found))
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        try:
            with Path(path).open("a") as handle:
                handle.write(f"### Queue rebuild {found['id']}\n\n```json\n{json.dumps(found, indent=2)}\n```\n")
        except OSError as exc:
            print(f"job summary not written: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
