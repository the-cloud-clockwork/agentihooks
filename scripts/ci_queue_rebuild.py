import argparse
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from scripts.swarm import merge_queue, queue_record
from scripts.swarm.store import SwarmError

QUERY = (
    """query($url: URI!) { resource(url: $url) { ... on PullRequest {
  timelineItems(last: 20, itemTypes: [ADDED_TO_MERGE_QUEUE_EVENT, REMOVED_FROM_MERGE_QUEUE_EVENT]) { nodes {
    __typename
    ... on AddedToMergeQueueEvent { createdAt actor { login } }
    ... on RemovedFromMergeQueueEvent { createdAt actor { login } reason }
  } }
  repository { mergeQueue(branch: "dev") { entries(first: 100) { nodes { """
    + queue_record.ENTRY
    + """ } } } }
} } }"""
)
UNSEEN = (
    "no entry ahead was enqueued and this pull request was not removed after the group started; "
    "a removal ahead, a base push or a ruleset change is not visible in queue entries"
)


def actor(event: dict) -> str | None:
    return (event.get("actor") or {}).get("login")


def cause(pull: str, started: str, groups: list[dict], events: list[dict]) -> dict:
    events = sorted((e for e in events if e["createdAt"] > started), key=lambda e: e["createdAt"])
    removals = [e for e in events if e["__typename"] == "RemovedFromMergeQueueEvent"]
    adds = [e for e in events if e["__typename"] == "AddedToMergeQueueEvent"]
    own = next((g for g in groups if g["pull"] == pull), None)
    if own is None:
        if not removals:
            return {"action": "unknown", "gap": "the pull request left the queue with no removal event"}
        last = removals[-1]
        return {"action": "dequeue", "actor": actor(last), "reason": last.get("reason"), "at": last["createdAt"]}
    if removals and adds and adds[-1]["createdAt"] >= removals[-1]["createdAt"]:
        return {
            "action": "requeue",
            "actor": actor(adds[-1]),
            "removed_reason": removals[-1].get("reason"),
            "at": adds[-1]["createdAt"],
        }
    ahead = [g for g in groups if g["position"] < own["position"] and g["enqueued_at"] > started]
    if not ahead:
        return {"action": "unknown", "gap": UNSEEN}
    return {
        "action": "priority enqueue" if any(g["jump"] for g in ahead) else "enqueue ahead",
        "ahead": [{key: g[key] for key in ("pull", "enqueuer", "enqueued_at", "jump")} for g in ahead],
    }


def record(observed: str, env, run, now: str | None = None) -> dict:
    repo = env["GITHUB_REPOSITORY"]
    pull = f"https://github.com/{repo}/pull/" + re.search(r"/pr-(\d+)-", env["GITHUB_REF"])[1]
    started, groups = None, []
    try:
        started = merge_queue.rest(f"repos/{repo}/actions/runs/{env['GITHUB_RUN_ID']}", run)["created_at"]
        found = merge_queue.graphql(QUERY, {"url": pull}, run)["resource"]
        groups = [queue_record.group(node) for node in found["repository"]["mergeQueue"]["entries"]["nodes"]]
        linked = cause(pull, started, groups, found["timelineItems"]["nodes"])
    except (SwarmError, KeyError, TypeError) as exc:
        linked = {"action": "unknown", "gap": f"queue unreadable: {exc}"}
    own = next((g for g in groups if g["pull"] == pull), {})
    return {
        "id": f"queue-rebuild-{env['GITHUB_RUN_ID']}-{env['GITHUB_RUN_ATTEMPT']}",
        "at": now or datetime.now(UTC).isoformat(),
        "ref": env["GITHUB_REF"],
        "pull": pull,
        "old_head": env["GITHUB_SHA"],
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
    elif action == "unknown":
        text = f"unknown cause: {linked['gap']}"
    else:
        text = "; ".join(f"{action} of {g['pull']} by {g['enqueuer']}" for g in linked["ahead"])
    return f"record {found['id']}: {text}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record why this merge queue group stopped being current")
    parser.add_argument("observed", nargs="?", default="")
    found = record(parser.parse_args(argv).observed, os.environ, subprocess.run)
    print(f"queue-rebuild {json.dumps(found)}", file=sys.stderr)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(path).open("a") as handle:
            handle.write(f"### Queue rebuild {found['id']}\n\n```json\n{json.dumps(found, indent=2)}\n```\n")
    print(summary(found))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
