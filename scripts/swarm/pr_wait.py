"""A swarm worker's push sets its checked wait: the hook marks each push from the git command, the tick declares the wait.

The PostToolUse hook, shared by every harness, marks a Bash call that pushes a task branch or queues its pull request.
The tick finds the task's open pull request, the recorded one or the forge's for the pushed branch, and declares a wait
on its merge while it sits in the merge queue, on its checks otherwise, so no agent relies on running swarm wait.
"""

import re
from collections.abc import Callable

from scripts.gates.progress import Progress
from scripts.swarm import idle, merge_queue, waits
from scripts.swarm.store import SwarmError

TRIGGERS = re.compile(
    r"(?:^|[;&|(\n]\s*)(?:gh\s+pr\s+(?:create|ready)\b|agentihooks\s+swarm\s+\S+\s+(?:publish|merge\s+queue)\b)"
)
GITHUB = re.compile(r"(?:^|github\.com[:/])([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
COVERS = ("checks", "merge")
FIELDS = ("pushed_at", "pushed_repo", "pushed_branch", "looked")
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


def mark(redis, slug: str, agent: str, at: int, repo: str, branch: str) -> None:
    key = Progress(redis, slug).key(agent)
    redis.hdel(key, "looked")
    redis.hset(key, mapping={"pushed_at": at, "pushed_repo": repo, "pushed_branch": branch})


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


def enforce_pass(
    store, slug: str, rows: dict, github: Callable, now_ms: int, forge: Callable[[str, str], str] = forge_pull
) -> list[str]:
    actions = []
    for agent in store.agents(slug):
        pushed = read(store.redis, slug, agent.name)
        if agent.state == "finished" or not pushed or agent.task not in rows:
            continue
        url = pull_of(store.redis, slug, agent.name, pushed, rows[agent.task], now_ms, forge)
        pull = github(url) if url and waits.PULL_URL.fullmatch(url) else None
        if pull is None:
            continue
        clear(store.redis, slug, agent.name)
        held = idle.wait(store.redis, slug, agent.name) or {}
        on = held.get("on") or {}
        if pull.state != "OPEN" or (on.get("kind") in COVERS and on.get("target") == url and held["until"] > now_ms):
            continue
        on = waits.on("merge" if pull.queued else "checks", url)
        if on["kind"] == "checks":
            on["head"] = pull.head
        idle.declare_wait(store.redis, slug, agent.name, now_ms + waits.CHECKED_MINUTES * 60_000, REASON, now_ms, on)
        actions.append(f"set the wait of {agent.name} on {on['kind']} of {url} from its push")
    return actions
