"""A swarm worker's push sets its checked wait: the hook marks each push from the git command, the tick declares the wait.

The PostToolUse hook, shared by every harness, marks a Bash call that pushes a task branch or queues its pull request,
and the head of a push of one commit to a lab grade ref. The tick finds the task's open pull request, the recorded one
or the forge's for the pushed branch, and declares a wait on its merge while it sits in the merge queue, on its checks
otherwise. With no pull request it declares a wait on the lab grade of the pushed head once the swarm's lab holds one,
so no agent relies on running swarm wait.
"""

import re
import sys
from collections.abc import Callable
from typing import Any

from scripts.gates.progress import Progress
from scripts.swarm import idle, lab_wait, merge_queue, waits
from scripts.swarm.store import SwarmError

TRIGGERS = re.compile(
    r"(?:^|[;&|(\n]\s*)(?:gh\s+pr\s+(?:create|ready)\b|agentihooks\s+swarm\s+\S+\s+(?:publish|merge\s+queue)\b)"
)
GITHUB = re.compile(r"(?:^|github\.com[:/])([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
COVERS = ("checks", "merge", "lab")
FIELDS = ("pushed_at", "pushed_repo", "pushed_branch", "pushed_head", "looked")
GRADE_REFS = "refs/grade/"
LOOKUP_MS = 5 * 60_000
REASON = "set by the swarm from the push of the task branch"
QUERY = """query($owner: String!, $name: String!, $branch: String!) {
  repository(owner: $owner, name: $name) {
    pullRequests(headRefName: $branch, states: OPEN, first: 1) { nodes { url } }
  }
}"""


def triggered(command: str | None) -> bool:
    from scripts.gates.one_push import actions

    if not command:
        return False
    return bool(TRIGGERS.search(command)) or any(kind == "push" for kind, _, _ in actions(command, None))


def mark(redis: Any, slug: str, agent: str, at: int, repo: str, branch: str, head: str) -> None:
    key = Progress(redis, slug).key(agent)
    redis.hdel(key, "looked", "pushed_head")
    pushed: dict[str, int | str] = {"pushed_at": at, "pushed_repo": repo, "pushed_branch": branch}
    if head:
        pushed["pushed_head"] = head
    redis.hset(key, mapping=pushed)


def read(redis, slug: str, agent: str) -> dict | None:
    raw = redis.hgetall(Progress(redis, slug).key(agent))
    return raw if raw.get("pushed_at") else None


def clear(redis, slug: str, agent: str) -> None:
    redis.hdel(Progress(redis, slug).key(agent), *FIELDS)


def pushed_where(command: str, cwd: str) -> tuple[str, str]:
    """The repository and branch a git push in the command writes, empty where it names none on GitHub."""
    from scripts.gates.one_push import actions, destinations, repo_of

    for kind, where, args in actions(command, cwd):
        if kind == "push":
            branches = sorted(destinations(where, args))
            return repo_of(where, args), branches[0] if len(branches) == 1 else ""
    return "", ""


def pushed_head(command: str, cwd: str) -> str:
    """The commit a git push in the command sends to its one lab grade ref, '' for any other push."""
    from scripts.gates.one_push import actions, positionals
    from scripts.gates.push_stop import git

    for kind, where, args in actions(command, cwd):
        if kind == "push":
            specs = positionals(args)[1:]
            if len(specs) != 1:
                return ""
            source, _, end = specs[0].removeprefix("+").rpartition(":")
            if not end.startswith(GRADE_REFS):
                return ""
            return git(where, "rev-parse", "--verify", f"{source or end}^{{commit}}").stdout.strip()
    return ""


def lab_grade(lab_url: str, head: str) -> str:
    """The grade url of the head on the lab at lab_url while the lab holds a grade of it, '' otherwise."""
    if not (lab_url and head):
        return ""
    url = f"{lab_url.removesuffix('/')}/grades/{head}"
    try:
        return url if lab_wait.grades(url) else ""
    except (OSError, ValueError):
        return ""


def forge_pull(repo: str, branch: str) -> str:
    """The open pull request whose head is the branch, '' when there is none or the forge cannot say."""
    found = GITHUB.search(repo)
    if not (found and branch):
        return ""
    try:
        data = merge_queue.graphql(QUERY, {"owner": found[1], "name": found[2], "branch": branch})
    except SwarmError:
        return ""
    nodes = data["repository"]["pullRequests"]["nodes"] if data.get("repository") else []
    return nodes[0]["url"] if nodes else ""


def pull_of(redis, slug, agent, pushed, task, now_ms, forge):
    if task.get("pr_url") or (pushed.get("looked") and now_ms - int(pushed["looked"]) < LOOKUP_MS):
        return task.get("pr_url")
    redis.hset(Progress(redis, slug).key(agent), "looked", now_ms)
    return forge(task.get("branch_repo") or pushed["pushed_repo"], task.get("branch") or pushed["pushed_branch"])


def either(forge: Callable[[str, str], str], lab: Callable[[str], str], head: str) -> Callable[[str, str], str]:
    """The forge lookup, falling back to the lab grade url of the pushed head when the forge finds no pull request."""
    return lambda repo, branch: forge(repo, branch) or (head and lab(head))


def pull_wait(pull: Any, url: str) -> dict[str, Any]:
    """The checked wait on an open pull request: on its merge while queued, on its checks at its head otherwise."""
    on = waits.on("merge" if pull.queued else "checks", url)
    if on["kind"] == "checks":
        on["head"] = pull.head
    return on


def enforce_pass(
    store: Any,
    slug: str,
    rows: dict[str, dict[str, Any]],
    github: Callable[[str], Any],
    now_ms: int,
    forge: Callable[[str, str], str] = forge_pull,
    grade: Callable[[str], str] | None = None,
) -> list[str]:
    """Declare each pushing agent's wait on its open pull request, else on the lab grade of its pushed head."""
    lab = grade or (lambda head: lab_grade(store.config(slug).lab_url, head))
    actions: list[str] = []
    for agent in store.agents(slug):
        try:
            pushed = read(store.redis, slug, agent.name)
            if agent.state == "finished" or not pushed or agent.task not in rows:
                continue
            lookup = either(forge, lab, pushed.get("pushed_head") or "")
            url = pull_of(store.redis, slug, agent.name, pushed, rows[agent.task], now_ms, lookup)
            pull = github(url) if url and waits.PULL_URL.fullmatch(url) else None
            if pull is None and not (url and lab_wait.GRADE_URL.fullmatch(url)):
                continue
            clear(store.redis, slug, agent.name)
            held = idle.wait(store.redis, slug, agent.name) or {}
            on = held.get("on") or {}
            closed = pull is not None and pull.state != "OPEN"
            if closed or (on.get("kind") in COVERS and on.get("target") == url and held["until"] > now_ms):
                continue
            on = waits.on("lab", url) if pull is None else pull_wait(pull, url)
            idle.declare_wait(
                store.redis, slug, agent.name, now_ms + waits.CHECKED_MINUTES * 60_000, REASON, now_ms, on
            )
            actions.append(f"set the wait of {agent.name} on {on['kind']} of {url} from its push")
        except Exception as exc:
            print(f"push wait of {agent.name} failed: {exc}", file=sys.stderr)
    return actions
