import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from scripts.swarm import merge_queue
from scripts.swarm.store import SwarmError

ENTRY = (
    "position state jump enqueuedAt enqueuer { login } headCommit { oid } baseCommit { oid }"
    " pullRequest { number url headRefOid }"
)
GROUPS = (
    """query($url: URI!, $after: String) { viewer { login } resource(url: $url) { ... on PullRequest {
  repository { mergeQueue(branch: "dev") { entries(first: 100, after: $after) {
    nodes { """
    + ENTRY
    + """ }
    pageInfo { hasNextPage endCursor }
  } } }
} } }"""
)


def group(node: dict) -> dict:
    return {
        "pull": node["pullRequest"]["url"],
        "pull_head": node["pullRequest"]["headRefOid"],
        "position": node["position"],
        "state": node["state"],
        "jump": node["jump"],
        "enqueued_at": node["enqueuedAt"],
        "enqueuer": (node["enqueuer"] or {}).get("login"),
        "head": (node["headCommit"] or {}).get("oid"),
        "base": (node["baseCommit"] or {}).get("oid"),
    }


def changes(before: list[dict], after: list[dict]) -> dict:
    old = {g["pull"]: g for g in before}
    new = {g["pull"]: g for g in after}
    return {
        "rebuilt": [
            {
                "pull": pull,
                "old_head": old[pull]["head"],
                "new_head": new[pull]["head"],
                "old_base": old[pull]["base"],
                "new_base": new[pull]["base"],
            }
            for pull in old
            if pull in new and old[pull]["head"] != new[pull]["head"]
        ],
        "removed": [pull for pull in old if pull not in new],
        "added": [pull for pull in new if pull not in old],
    }


class Journal:
    def __init__(self, url: str, run=subprocess.run, environ=os.environ, clock=lambda: datetime.now(UTC), context=None):
        self.url = url
        self.context = context or {}
        self.run = run
        self.environ = environ
        self.clock = clock
        self.actor = None
        self.gaps = []
        self.read_at = []

    def groups(self) -> list[dict] | None:
        found, variables = [], {"url": self.url}
        self.read_at.append(self.clock().isoformat())
        try:
            while True:
                data = merge_queue.graphql(GROUPS, variables, self.run)
                self.actor = data["viewer"]["login"]
                page = data["resource"]["repository"]["mergeQueue"]["entries"]
                found += [group(node) for node in page["nodes"]]
                if not page["pageInfo"]["hasNextPage"]:
                    return sorted(found, key=lambda g: g["position"])
                variables["after"] = page["pageInfo"]["endCursor"]
        except (SwarmError, KeyError, TypeError) as exc:
            self.gaps.append(f"queue unreadable: {exc}")
            return None

    def write(
        self,
        action: str,
        pull: str,
        head: str,
        before: list[dict] | None,
        after: list[dict] | None,
        errors=(),
        **extra,
    ) -> dict:
        record = {
            "id": uuid.uuid4().hex,
            "at": self.clock().isoformat(),
            "action": action,
            "token_actor": self.actor,
            "agent": self.environ.get("AGENTIHOOKS_AGENT_NAME"),
            "pull": pull,
            "expected_head": head,
            "old_groups": before,
            "new_groups": after,
            "observed_between": self.read_at[:1] + self.read_at[-1:],
            **(changes(before, after) if before is not None and after is not None else {}),
            "gaps": self.gaps,
            "errors": list(errors),
            **self.context,
            **extra,
        }
        path = Path(self.environ.get("AGENTIHOOKS_HOME") or Path.home() / ".agentihooks") / "merge-queue-events.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as handle:
                handle.write(json.dumps(record) + "\n")
        except OSError as exc:
            print(f"merge queue record not written: {exc}", file=sys.stderr)
        return record
