"""Done on a code or ci task needs its pull request merged.

`swarm done` refuses until GitHub reads the pull request as merged. The tick re-reads the tasks a lane agent marked
done in the last day and reopens one whose pull request is not merged; a done from the master or operator stands.
"""

from scripts.swarm.naming import lane_of
from scripts.swarm_ledger import ledger_kinds

GATED = ("code", "ci")
WORKERS = ("eng", "ci")
MERGED = "MERGED"
RECHECK_MS = 24 * 60 * 60_000
SEEN_TTL_S = 2 * 24 * 3600
REOPENED = (
    "Reopened by the swarm: its pull request {url} is {state}, not merged. "
    "A code task is done only when its pull request merges."
)


def refusal(task, url, github):
    """Why `swarm done` must refuse this task, or '' when it may close."""
    kind = ledger_kinds.kind(task)
    if kind not in GATED:
        return ""
    if task.get("split"):
        return _split_refusal(task, url, github)
    if not url:
        return f"a {kind} task is done only with its merged pull request: give --pr <url>"
    return _unmerged(url, github)


def _unmerged(url, github):
    found = github(url)
    if found is None:
        return f"could not read pull request {url} from GitHub; run swarm done again when it answers"
    if found.state != MERGED:
        return f"pull request {url} is {found.state.lower()}, not merged; merge it, then run swarm done again"
    return ""


def _split_refusal(task, url, github):
    bound = _pulls(task, url)
    if len(bound) < len(task["split"]):
        return (
            f"half {len(bound) + 1} of this split task has no pull request yet: record it with swarm pr <url>, "
            "merge it, then run swarm done again"
        )
    for number, pull in enumerate(bound, 1):
        if refused := _unmerged(pull, github):
            return f"half {number} of this split task: {refused}"
    return ""


def require_local(store: object, slug: str, task_id: str) -> None:
    from scripts.swarm.store import SwarmError

    if store.redis.exists(store.key(slug, "task-authority", task_id), store.key(slug, "claim-journal", task_id)):
        raise SwarmError("distributed final mutations require the controller outcome path")


def require_target(store: object, slug: str, task_id: str, url: str, tasks: list[dict]) -> None:
    from scripts.swarm.store import SwarmError

    own = next((task for task in tasks if task["id"] == task_id), {})
    if url not in _pulls(own):
        raise SwarmError("final integration must target this task's recorded pull request")
    for task in tasks:
        if task.get("pr_url") == url:
            require_local(store, slug, task["id"])


def recheck_pass(store, slug, doc, ledger, now_ms, github):
    tasks = {t["id"]: t for t in doc.get("tasks", [])}
    actions = []
    for task_id in _done_by_workers(doc.get("_meta", {}).get("events", []), now_ms):
        task = tasks.get(task_id, {})
        if task.get("state") != "done" or ledger_kinds.kind(task) not in GATED or not task.get("pr_url"):
            continue
        for url in _pulls(task):
            state = _unmerged_state(store, slug, url, github)
            if state:
                ledger.update_task(slug, task_id, {"state": "open", "claimed_by": ""})
                ledger.comment(slug, task_id, REOPENED.format(url=url, state=state), by="swarm")
                actions.append(f"task {task_id} reopened, its pull request is {state}")
                break
    return actions


def _pulls(task, url=None):
    url = url or task.get("pr_url")
    if not task.get("split"):
        return [url]
    bound = list(dict.fromkeys([*(task.get("split_pulls") or []), *([url] if url else [])]))
    return bound[: len(task["split"])]


def _unmerged_state(store, slug, url, github):
    seen = store.key(slug, "done-merged", url)
    if store.redis.exists(seen):
        return ""
    found = github(url)
    if found is None:
        return ""
    if found.state == MERGED:
        store.redis.set(seen, 1, ex=SEEN_TTL_S)
        return ""
    return found.state.lower()


def _done_by_workers(events, now_ms):
    latest = {}
    for event in events:
        if event["kind"] == "task done" and event["target"].startswith("tasks/"):
            latest[event["target"].removeprefix("tasks/")] = event
    return [
        task_id
        for task_id, event in latest.items()
        if now_ms - event["at"] <= RECHECK_MS and lane_of(event["by"]) in WORKERS
    ]
