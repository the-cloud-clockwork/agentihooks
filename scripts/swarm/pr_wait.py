"""A swarm worker's push sets its checked wait: the hook marks each push from the git command, the tick declares the wait.

The PostToolUse hook, shared by every harness, marks a Bash call that pushes a task branch or queues its pull request.
The tick finds the task's open pull request, the recorded one or the forge's for the pushed branch, and declares a wait
on its merge while it sits in the merge queue, on its checks otherwise, so no agent relies on running swarm wait.
"""

import re

from scripts.swarm import idle, merge_queue, publish_queue, waits
from scripts.swarm.store import SwarmError

TRIGGERS = re.compile(
    r"(?:^|[;&|(\n]\s*)(?:git(?:\s+-C\s+\S+)?\s+push\b|gh\s+pr\s+(?:create|ready)\b"
    r"|agentihooks\s+swarm\s+\S+\s+(?:publish|merge\s+queue)\b)"
)
TTL_S = 24 * 60 * 60
LOOKUP_MS = 5 * 60_000
REASON = "set by the swarm from the push of the task branch"
QUERY = """query($owner: String!, $name: String!, $branch: String!) {
  repository(owner: $owner, name: $name) {
    pullRequests(headRefName: $branch, states: OPEN, first: 1) { nodes { url } }
  }
}"""


def triggered(command: str | None) -> bool:
    return bool(command and TRIGGERS.search(command))


def key(redis, slug: str, agent: str) -> str:
    from scripts.hive import scope

    return f"{scope.readings(redis, slug, agent)}:{slug}:pushed:{agent}"


def mark(redis, slug: str, agent: str, at: int, repo: str = "", branch: str = "") -> None:
    name = key(redis, slug, agent)
    redis.delete(name)
    redis.hset(name, mapping={"at": at, "repo": repo, "branch": branch})
    redis.expire(name, TTL_S)


def read(redis, slug: str, agent: str) -> dict | None:
    raw = redis.hgetall(key(redis, slug, agent))
    return raw if raw.get("at") else None


def pushed_where(command: str, cwd: str) -> tuple[str, str]:
    """The repository and branch a git push in the command writes, empty where it names none on GitHub."""
    from scripts.gates.one_push import actions, destinations, repo_of

    for kind, where, args in actions(command, cwd):
        if kind == "push":
            branches = sorted(destinations(where, args) - {""})
            return repo_of(where, args), branches[0] if len(branches) == 1 else ""
    return "", ""


def forge_pull(repo: str, branch: str) -> str:
    """The open pull request whose head is the branch, '' when there is none or the forge cannot say."""
    if "://" in repo or "@" in repo:
        try:
            owner, name = publish_queue.github(repo) or ("", "")
        except ValueError:
            owner, name = "", ""
    else:
        owner, _, name = repo.partition("/")
    if not (owner and name and branch):
        return ""
    try:
        data = merge_queue.graphql(QUERY, {"owner": owner, "name": name, "branch": branch})
    except SwarmError:
        return ""
    nodes = ((data.get("repository") or {}).get("pullRequests") or {}).get("nodes") or []
    return nodes[0]["url"] if nodes else ""


def pull_of(redis, slug, agent, pushed, task, now_ms, forge):
    if task.get("pr_url"):
        return task["pr_url"]
    if pushed.get("looked") and now_ms - int(pushed["looked"]) < LOOKUP_MS:
        return ""
    redis.hset(key(redis, slug, agent), "looked", now_ms)
    repo = task.get("branch_repo") or pushed.get("repo", "")
    return forge(repo, task.get("branch") or pushed.get("branch", ""))


def enforce_pass(store, slug: str, rows: dict, github, now_ms: int, forge=forge_pull) -> list[str]:
    actions = []
    for agent in store.agents(slug):
        pushed = read(store.redis, slug, agent.name)
        if agent.state == "finished" or not pushed or agent.task not in rows:
            continue
        held = idle.wait(store.redis, slug, agent.name)
        if held and held.get("on") and held["until"] > now_ms:
            store.redis.delete(key(store.redis, slug, agent.name))
            continue
        url = pull_of(store.redis, slug, agent.name, pushed, rows[agent.task], now_ms, forge)
        pull = github(url) if url and waits.PULL_URL.fullmatch(url) else None
        if pull is None:
            continue
        store.redis.delete(key(store.redis, slug, agent.name))
        if pull.state != "OPEN":
            continue
        on = waits.on("merge" if pull.queued else "checks", url)
        if on["kind"] == "checks":
            on["head"] = pull.head
        idle.declare_wait(store.redis, slug, agent.name, now_ms + waits.CHECKED_MINUTES * 60_000, REASON, now_ms, on)
        actions.append(f"set the wait of {agent.name} on {on['kind']} of {url} from its push")
    return actions
