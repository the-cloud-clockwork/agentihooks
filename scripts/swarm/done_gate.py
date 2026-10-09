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
    if not url:
        return f"a {kind} task is done only with its merged pull request: give --pr <url>"
    found = github(url)
    if found is None:
        return f"could not read pull request {url} from GitHub; run swarm done again when it answers"
    if found.state != MERGED:
        return f"pull request {url} is {found.state.lower()}, not merged; merge it, then run swarm done again"
    return ""


def require_local(store: object, slug: str, task_id: str) -> None:
    from scripts.swarm.store import SwarmError

    if store.redis.exists(store.key(slug, "task-authority", task_id), store.key(slug, "claim-journal", task_id)):
        raise SwarmError("distributed final mutations require the controller outcome path")


def recheck_pass(store, slug, doc, ledger, now_ms, github):
    tasks = {t["id"]: t for t in doc.get("tasks", [])}
    actions = []
    for task_id in _done_by_workers(doc.get("_meta", {}).get("events", []), now_ms):
        task = tasks.get(task_id, {})
        url = task.get("pr_url")
        if task.get("state") != "done" or ledger_kinds.kind(task) not in GATED or not url:
            continue
        seen = store.key(slug, "done-merged", url)
        if store.redis.exists(seen):
            continue
        found = github(url)
        if found is None:
            continue
        if found.state == MERGED:
            store.redis.set(seen, 1, ex=SEEN_TTL_S)
            continue
        state = found.state.lower()
        ledger.update_task(slug, task_id, {"state": "open", "claimed_by": ""})
        text = REOPENED.format(url=url, state=state)
        ledger.comment(slug, task_id, text, by="swarm")
        actions.append(f"task {task_id} reopened, its pull request is {state}")
    return actions


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
