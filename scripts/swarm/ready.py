"""swarm ready: an engineer hands its reviewed pull request to the tick and frees its seat.

The merge pass watches the pull request's checks, queues it on green, waits for the merge, then writes the done fields
swarm done writes and removes the worktree. Red checks, a closed pull request or a queue ejection reopen the task for
the next engineer, who continues from the stored handoff.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from scripts.swarm import claim_order, done_gate, linked_issues, merge_queue, stack, waits
from scripts.swarm.store import ASSIST, MANUAL, AgentRecord, RedisStore, SwarmError
from scripts.swarm_ledger import ledger_kinds

AWAITING = "merge"
FIX = claim_order.FIX
CONFLICT = "conflicts with dev and needs an update the swarm cannot make"
RECORDS = "ready"
GATED = ("code", "ci")
QUEUE_TRIES = 5
REFUSALS = ("GraphQL", "swarm merge queue operations require")
HOLDS = ("timed out", "error connecting to api.github.com", "HTTP 5", "the current pull request head must pass Tests")
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


def check(
    row: dict, agent: AgentRecord, autonomy: str, github: Callable, now_ms: int, previous: dict | None = None
) -> dict:
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
    draft = stack.shell(["gh", "pr", "view", url, "--json", "isDraft", "--jq", ".isDraft"])
    if draft.returncode != 0:
        raise SwarmError(f"cannot read whether pull request {url} is a draft; run swarm ready again when it answers")
    if draft.stdout.strip() == "true":
        raise SwarmError(f"pull request {url} is a draft; mark it ready for review with gh pr ready first")
    top = _checkout(url, pull.head, previous)
    found = held_record(url, pull.head, top, agent.name, now_ms)
    if pull.queued:
        found["on"], found["queued_at"] = {"kind": "merge", "target": url}, now_ms
    return found


def _checkout(url, head, previous):
    try:
        top = stack.worktree()
    except SwarmError:
        if not previous or previous.get("url") != url or previous.get("head") != head:
            raise
        return previous.get("worktree", "")
    if _git("status", "--porcelain"):
        raise SwarmError("the worktree holds uncommitted changes; commit and push them first")
    if _git("rev-parse", "HEAD") != head:
        raise SwarmError("the pull request head is not this worktree's head; push your last commit before swarm ready")
    return top


def continuation(store: RedisStore, slug: str, task_id: str) -> dict | None:
    previous = record(store, slug, task_id)
    if previous:
        return previous
    envelope = store.handoff_envelope(slug, task_id) or {}
    pull = envelope.get("pull_request")
    if not isinstance(pull, dict):
        return None
    top = envelope.get("worktree", "")
    return {
        "url": pull.get("url"),
        "head": envelope.get("remote_head"),
        "worktree": top if Path(top).is_absolute() else "",
    }


def release(store, slug, task_id, found, ledger, by, github):
    from scripts.swarm import idle

    held = idle.wait(store.redis, slug, by)
    if held and held["until"] > found["at"]:
        on = held.get("on", {})
        rows = waits.named_tasks(on.get("kind"), on.get("target"), task_id, lambda wanted: ledger.task(slug, wanted))
        if waits.dependency(on, {"id": task_id, "pr_url": found["url"]}, rows, github):
            found["dependency"] = on
    save(store, slug, task_id, found)
    ledger.update_task(slug, task_id, {"state": "pr", "awaiting": AWAITING}, by=by)


def withdraw(store, slug, task_id, ledger, by):
    forget(store, slug, task_id)
    ledger.update_task(slug, task_id, {"awaiting": ""}, by=by)


@dataclass(frozen=True)
class Pass:
    store: object
    slug: str
    ledger: object
    github: Callable
    reread: Callable
    now_ms: int
    queue: Callable = merge_queue.operate


def merge_pass(run, rows):
    actions = []
    for task_id, row in rows.items():
        if row.get("awaiting") != AWAITING:
            if row.get("state") == "pr" and row.get("pr_url"):
                pull = run.github(row["pr_url"])
                if pull is not None and pull.state == "OPEN" and pull.merge_problem:
                    found = {"url": row["pr_url"], "agent": row.get("claimed_by")}
                    actions += _unmergeable(run, row, found, pull)
            continue
        found = record(run.store, run.slug, task_id)
        if not awaiting(row) or found is None or found["agent"] != row.get("claimed_by"):
            actions += _leave(run, task_id, row)
            continue
        actions += _advance(run, rows, row, found)
    return actions


def _leave(run, task_id, row):
    run.ledger.update_task(run.slug, task_id, {"awaiting": ""})
    forget(run.store, run.slug, task_id)
    who = row.get("claimed_by")
    return [f"task {task_id} left the merge pass" + (f": {who} works it" if who else "")]


def _advance(run, rows, row, found):
    url, held = found["url"], found["on"]
    pull = run.github(url)
    if pull is None:
        return []
    if pull.state == "MERGED":
        return _finish(run, rows, row, found)
    if pull.state != "OPEN":
        return _reopen(run, row, found, "was closed without merging")
    if waits.dependency(found.get("dependency", {}), row, rows, run.github):
        return []
    if pull.merge_problem:
        return _unmergeable(run, row, found, pull)
    if held["kind"] == "checks":
        if pull.mergeable == "CONFLICTING":
            return _reopen(run, row, found, CONFLICT)
        return _checks(run, row, found, pull)
    outcome = waits.merge_resolution(held, run.github, run.reread, run.now_ms - found["queued_at"] < waits.FRESH_MS)
    if not outcome:
        save(run.store, run.slug, row["id"], found)
        return []
    if outcome == f"pull request {url}, now merged":
        return _finish(run, rows, row, found)
    return _reopen(run, row, found, "left the merge queue without merging")


def _checks(run, row, found, pull):
    outcome = waits.checks_resolution(found["on"], run.github)
    if not outcome.startswith(f"checks on {found['url']}"):
        save(run.store, run.slug, row["id"], found)
        return []
    if outcome.startswith(f"checks on {found['url']}, now red"):
        steps = ", ".join(pull.failed)
        return _reopen(run, row, found, f"has red checks: {steps}" if steps else "has red checks")
    return _queue(run, row, found)


def _queue(run, row, found):
    task_id, url = row["id"], found["url"]
    try:
        result = run.queue("queue", url)
    except SwarmError as exc:
        if str(exc).startswith(REFUSALS):
            found["refusals"] = found.get("refusals", 0) + 1
            if found["refusals"] >= QUEUE_TRIES:
                return _reopen(run, row, found, f"could not be queued: {exc}")
        elif not any(hold in str(exc) for hold in HOLDS):
            return _reopen(run, row, found, f"could not be queued: {exc}")
        save(run.store, run.slug, task_id, found)
        return [f"could not queue {url} for task {task_id}: {exc}"]
    if result.get("waiting") == "checks":
        found["on"] = {"kind": "checks", "target": url, "previous_head": result["previous_head"]}
        save(run.store, run.slug, task_id, found)
        return [f"{url} for task {task_id} updated from dev; waiting on its checks again"]
    found["on"], found["queued_at"] = {"kind": "merge", "target": url}, run.now_ms
    save(run.store, run.slug, task_id, found)
    return [f"queued {url} for task {task_id}"]


def _finish(run, rows, row, found):
    task_id, url = row["id"], found["url"]
    if row.get("split") and done_gate.refusal(row, url, run.github):
        return []
    fields = {"state": "done", "pr_url": url, "awaiting": ""}
    run.ledger.update_task(run.slug, task_id, fields)
    finished = [task_id]
    for member in row.get("group_members") or []:
        if member in rows and rows[member].get("state") != "done":
            run.ledger.update_task(run.slug, member, fields)
            finished.append(member)
    run.store.clear_handoff(run.slug, task_id)
    forget(run.store, run.slug, task_id)
    return [f"task {task_id} done, {url} merged", *linked_issues.settle(url, finished, rows), *_remove(found)]


def _unmergeable(run, row, found, pull):
    if pull.queued:
        try:
            result = run.queue("dequeue", found["url"])
        except SwarmError as exc:
            return [f"could not dequeue {found['url']} for task {row['id']}: {exc}"]
        if result.get("queued") or result.get("state") == "MERGED":
            return [f"dequeue of {found['url']} for task {row['id']} awaits confirmation"]
    return _reopen(run, row, found, pull.merge_problem)


def _reopen(run, row, found, what):
    task_id, url = row["id"], found["url"]
    run.ledger.update_task(run.slug, task_id, {"state": "open", "claimed_by": "", "awaiting": FIX}, if_state=("pr",))
    run.ledger.comment(run.slug, task_id, REOPENED.format(url=url, what=what), by="swarm")
    run.store.release(run.slug, task_id, found["agent"])
    forget(run.store, run.slug, task_id)
    removed = _remove(found, "--pushed") if found.get("worktree") else []
    return [f"task {task_id} reopened, {url} {what}", *removed]


def _remove(found, *flags):
    top = found["worktree"]
    if not top:
        return []
    done = stack.shell(["bash", str(stack.WT_SCRIPT), "done", Path(top).name, "--repo", top, *flags])
    if done.returncode != 0:
        return [f"could not remove the worktree {top}: {done.stderr.strip()}"]
    return [f"removed the worktree {top}"]
