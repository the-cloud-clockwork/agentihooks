"""swarm ready: an engineer hands its reviewed pull request to the tick and frees its seat.

The merge pass watches the pull request's checks, queues it on green, waits for the merge, then writes the done fields
swarm done writes and removes the worktree. Red checks, a closed pull request or a queue ejection reopen the task for
the next engineer, who continues from the stored handoff.
"""

import json
from pathlib import Path

from scripts.swarm import merge_queue, stack, waits
from scripts.swarm.store import ASSIST, MANUAL, SwarmError
from scripts.swarm_ledger import ledger_kinds

AWAITING = "merge"
RECORDS = "ready"
GATED = ("code", "ci")
REOPENED = (
    "Reopened by the swarm: its pull request {url} {what}. The next engineer continues it from the handoff its "
    "engineer left."
)


def awaiting(task):
    return task.get("state") == "pr" and task.get("awaiting") == AWAITING


def record(store, slug, task_id):
    raw = store.redis.hget(store.key(slug, RECORDS), task_id)
    return json.loads(raw) if raw else None


def save(store, slug, task_id, found):
    store.redis.hset(store.key(slug, RECORDS), task_id, json.dumps(found))


def forget(store, slug, task_id):
    store.redis.hdel(store.key(slug, RECORDS), task_id)


def held_record(url, head, worktree, agent, at):
    return {
        "url": url,
        "head": head,
        "worktree": worktree,
        "agent": agent,
        "at": at,
        "on": {"kind": "checks", "target": url, "head": head},
    }


def _git(*args):
    done = stack.shell(["git", *args])
    if done.returncode != 0:
        raise SwarmError(f"git {args[0]} failed: {done.stderr.strip()}")
    return done.stdout.strip()


def check(row, agent, autonomy, github, now_ms):
    """The record of a task that may go ready, or SwarmError naming why it may not."""
    if autonomy in (MANUAL, ASSIST):
        raise SwarmError(
            f"this swarm runs at {autonomy} autonomy, where the operator approves each merge; "
            "follow the ship steps in your prompt"
        )
    kind = ledger_kinds.kind(row)
    if kind not in GATED:
        raise SwarmError(
            f"swarm ready carries a code or ci pull request; this {kind} task closes with swarm done and its proof"
        )
    url = row.get("pr_url")
    if not url:
        raise SwarmError("record the pull request with swarm pr before swarm ready")
    pull = github(url)
    if pull is None or not pull.head:
        raise SwarmError(f"cannot read pull request {url} from GitHub; run swarm ready again when it answers")
    if pull.state == "MERGED":
        raise SwarmError(f"pull request {url} is merged; close the task with swarm done")
    if pull.state != "OPEN":
        raise SwarmError(f"pull request {url} is {pull.state.lower()}; swarm ready carries an open pull request")
    top = stack.worktree()
    if _git("status", "--porcelain"):
        raise SwarmError("the worktree holds uncommitted changes; commit and push them first")
    if _git("rev-parse", "HEAD") != pull.head:
        raise SwarmError("the pull request head is not this worktree's head; push your last commit before swarm ready")
    return held_record(url, pull.head, top, agent.name, now_ms)


def release(store, slug, task_id, found, ledger, by):
    save(store, slug, task_id, found)
    ledger.update_task(slug, task_id, {"state": "pr", "awaiting": AWAITING}, by=by)


def merge_pass(store, slug, rows, ledger, github, reread, now_ms, queue=merge_queue.operate):
    actions = []
    for task_id, row in rows.items():
        if row.get("awaiting") != AWAITING:
            continue
        found = record(store, slug, task_id)
        if not awaiting(row) or found is None or found["agent"] != row.get("claimed_by"):
            actions += _leave(store, slug, task_id, row, ledger)
            continue
        actions += _advance(store, slug, rows, row, found, ledger, github, reread, now_ms, queue)
    return actions


def _leave(store, slug, task_id, row, ledger):
    ledger.update_task(slug, task_id, {"awaiting": ""})
    forget(store, slug, task_id)
    who = row.get("claimed_by")
    return [f"task {task_id} left the merge pass" + (f": {who} works it" if who else "")]


def _advance(store, slug, rows, row, found, ledger, github, reread, now_ms, queue):
    url, held = found["url"], found["on"]
    pull = github(url)
    if pull is None:
        return []
    if pull.state == "MERGED":
        return _finish(store, slug, rows, row, found, ledger)
    if held["kind"] == "checks":
        if not waits.checks_resolution(held, github):
            save(store, slug, row["id"], found)
            return []
        if pull.state != "OPEN":
            return _reopen(store, slug, row, found, ledger, "was closed without merging")
        if pull.red:
            return _reopen(store, slug, row, found, ledger, "has red checks")
        return _queue(store, slug, row["id"], found, now_ms, queue)
    outcome = waits.merge_resolution(held, github, reread, now_ms - found["queued_at"] < waits.FRESH_MS)
    if not outcome:
        save(store, slug, row["id"], found)
        return []
    if outcome == f"pull request {url}, now merged":
        return _finish(store, slug, rows, row, found, ledger)
    return _reopen(store, slug, row, found, ledger, "left the merge queue without merging")


def _queue(store, slug, task_id, found, now_ms, queue):
    url = found["url"]
    try:
        result = queue("queue", url)
    except SwarmError as exc:
        return [f"could not queue {url} for task {task_id}: {exc}"]
    if result.get("waiting") == "checks":
        found["on"] = {"kind": "checks", "target": url, "previous_head": result["previous_head"]}
        save(store, slug, task_id, found)
        return [f"{url} for task {task_id} updated from dev; waiting on its checks again"]
    found["on"], found["queued_at"] = {"kind": "merge", "target": url}, now_ms
    save(store, slug, task_id, found)
    return [f"queued {url} for task {task_id}"]


def _finish(store, slug, rows, row, found, ledger):
    task_id, url = row["id"], found["url"]
    fields = {"state": "done", "pr_url": url, "awaiting": ""}
    ledger.update_task(slug, task_id, fields)
    for member in row.get("group_members") or []:
        if member in rows and rows[member].get("state") != "done":
            ledger.update_task(slug, member, fields)
    store.clear_handoff(slug, task_id)
    forget(store, slug, task_id)
    return [f"task {task_id} done, {url} merged", *_remove(found)]


def _reopen(store, slug, row, found, ledger, what):
    task_id, url = row["id"], found["url"]
    ledger.update_task(slug, task_id, {"state": "open", "claimed_by": "", "awaiting": ""}, if_state=("pr",))
    ledger.comment(slug, task_id, REOPENED.format(url=url, what=what), by="swarm")
    forget(store, slug, task_id)
    return [f"task {task_id} reopened, {url} {what}", *_remove(found, "--pushed")]


def _remove(found, *flags):
    top = found.get("worktree")
    if not top:
        return []
    done = stack.shell(["bash", str(stack.WT_SCRIPT), "done", Path(top).name, "--repo", top, *flags])
    if done.returncode != 0:
        return [f"could not remove the worktree {top}: {done.stderr.strip()}"]
    return [f"removed the worktree {top}"]
