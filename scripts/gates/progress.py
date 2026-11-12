"""The one progress signal: each swarm agent's last outcome and the talk writes it made since, kept in Redis."""

import re
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

OUTCOME_COMMANDS = (
    ("pushed", re.compile(r"(?:^|[;&|(\n]\s*)git(?:\s+-C\s+\S+)?\s+push\b")),
    ("pull request opened", re.compile(r"(?:^|[;&|(\n]\s*)gh\s+pr\s+create\b")),
)
HELD = ("claimed", "pr")
HEAD = "head"
LOOKED = "head_looked"
LOOKUP_MS = 5 * 60_000
BRANCH_QUERY = """query($owner: String!, $name: String!, $branch: String!, $ref: String!) {
  repository(owner: $owner, name: $name) {
    ref(qualifiedName: $ref) { target { oid } }
    pullRequests(headRefName: $branch, states: OPEN, first: 1) { nodes { url } }
  }
}"""


@dataclass(frozen=True)
class Mark:
    outcome_at: int = 0
    outcome: str = ""
    talk: int = 0


def outcome_of(command):
    return next((kind for kind, pattern in OUTCOME_COMMANDS if command and pattern.search(command)), "")


class Progress:
    def __init__(self, redis, slug):
        self.redis, self.slug = redis, slug

    def key(self, agent):
        from scripts.hive import scope

        return f"{scope.readings(self.redis, self.slug, agent)}:{self.slug}:progress:{agent}"

    def read(self, agent):
        raw = self.redis.hgetall(self.key(agent))
        return Mark(int(raw.get("outcome_at") or 0), raw.get("outcome", ""), int(raw.get("talk") or 0))

    def talk(self, agent):
        return int(self.redis.hincrby(self.key(agent), "talk"))

    def outcome(self, agent, kind, now_ms=None):
        at = int(time.time() * 1000) if now_ms is None else now_ms
        self.redis.hset(self.key(agent), mapping={"outcome_at": at, "outcome": kind, "talk": 0})

    def outcome_once(self, agent, kind, resolution, now_ms=None):
        if self.redis.hget(self.key(agent), "resolved") == resolution:
            return False
        self.redis.hset(self.key(agent), "resolved", resolution)
        self.outcome(agent, kind, now_ms)
        return True


def checks_pass(redis, slug, tasks, github, now_ms=None):
    marks, stamped = Progress(redis, slug), []
    for task in tasks:
        url, owner = task.get("pr_url"), task.get("claimed_by")
        if task.get("state") != "pr" or not (url and owner):
            continue
        found = github(url)
        if found is None or not found.resolved:
            continue
        verdict = "red" if found.red else "green"
        if marks.outcome_once(owner, "checks resolved", f"{url} {found.pushed_at} {verdict}", now_ms):
            stamped.append(f"checks resolved {verdict} on {url}, an outcome for {owner}")
    return stamped


def origin(path: str, run: Callable[..., Any] = subprocess.run) -> str:
    """The public address of the origin remote of the checkout at ``path``, empty where it has none."""
    if not path:
        return ""
    done = run(["git", "-C", path, "remote", "get-url", "origin"], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        return ""
    from scripts.swarm import stack

    return stack.public_url(done.stdout.strip())


def branch_head(repo: str, branch: str) -> tuple[str, str] | None:
    """The newest commit of the branch on GitHub and its open pull request, empty where there is none, None unread."""
    from scripts.swarm import merge_queue, pr_wait
    from scripts.swarm.store import SwarmError

    found = pr_wait.GITHUB.search(repo)
    if not (found and branch):
        return None
    variables = {"owner": found[1], "name": found[2], "branch": branch, "ref": f"refs/heads/{branch}"}
    try:
        data = merge_queue.graphql(BRANCH_QUERY, variables)
    except SwarmError:
        return None
    repository = data.get("repository") or {}
    pulls = (repository.get("pullRequests") or {}).get("nodes") or []
    target = (repository.get("ref") or {}).get("target") or {}
    return target.get("oid") or "", pulls[0]["url"] if pulls else ""


def _facts(
    marks: Progress,
    owner: str,
    task: dict[str, Any],
    reads: tuple[Callable[[str], Any], Callable[[str, str], tuple[str, str] | None]],
    now_ms: int,
    repo: str,
) -> tuple[str, str] | None:
    """The head and pull request GitHub shows for the task now, None when it was not read this pass."""
    github, branch = reads
    url = task.get("pr_url") or ""
    if url:
        pull = github(url)
        return (pull.head, url) if pull is not None and pull.head else None
    where, stamp = task.get("branch_repo") or repo, f"{LOOKED}:{task['id']}"
    looked = marks.redis.hget(marks.key(owner), stamp)
    if not where or (looked and now_ms - int(looked) < LOOKUP_MS):
        return None
    marks.redis.hset(marks.key(owner), stamp, now_ms)
    from scripts.swarm import naming

    return branch(where, task.get("branch") or naming.plain(owner))


def _change(seen: str, task_id: str, head: str, url: str) -> list[str]:
    """The outcomes a move from the recorded ``seen`` fact to this head and pull request is, a push last."""
    known, _, rest = seen.partition(" ")
    if known != task_id:
        return []
    before, _, opened = rest.partition(" ")
    return [
        kind
        for kind, moved in (("pull request opened", url and not opened), ("pushed", head and head != before))
        if moved
    ]


def heads_pass(
    redis: Any,
    slug: str,
    tasks: Iterable[dict[str, Any]],
    github: Callable[[str], Any],
    now_ms: int,
    repo: str = "",
    branch: Callable[[str, str], tuple[str, str] | None] = branch_head,
) -> list[str]:
    """Record a new head or pull request GitHub shows for each held task as an outcome of its claimant, whatever
    harness or command pushed it; the first sight of a task is its baseline."""
    marks, stamped = Progress(redis, slug), []
    for task in tasks:
        owner = task.get("claimed_by")
        if task.get("state") not in HELD or not owner:
            continue
        found = _facts(marks, owner, task, (github, branch), now_ms, repo)
        if found is None:
            continue
        head, url = found
        seen = redis.hget(marks.key(owner), HEAD) or ""
        redis.hset(marks.key(owner), HEAD, f"{task['id']} {head} {url}")
        for kind in _change(seen, task["id"], head, url):
            marks.outcome(owner, kind, now_ms)
            stamped.append(
                f"{kind} {head} on {url or task.get('branch') or 'its worktree branch'}, an outcome for {owner}"
            )
    return stamped
