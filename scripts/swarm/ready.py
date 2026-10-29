"""swarm ready: an engineer hands its reviewed pull request to the tick and frees its seat.

The merge pass watches the pull request's checks, queues it on green, waits for the merge, then writes the done fields
swarm done writes and removes the worktree; a task of a kind other than code or ci is left open instead, with its
pull request and handoff kept, for the proof its kind needs. Red checks, a closed pull request or a queue ejection
reopen the task for the next engineer, who continues from the stored handoff.
"""

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from scripts.inbox.store import DuplicateItem, InboxStore
from scripts.swarm import (
    claim_order,
    done_gate,
    idle,
    linked_issues,
    merge_queue,
    queue_exit,
    queue_record,
    stack,
    staged_compatibility,
    waits,
)
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
REQUEUED = (
    "The swarm queued {url} again: it left the merge queue while green and no merge group run of its own failed, "
    "so the task keeps its claim."
)
HEAD_RED = "left the merge queue red"
OWN_RED = "left the merge queue red in its own merge group run"
UNREAD_EXIT = "left the merge queue green and its merge group runs could not all be read"
DROPPED_GREEN = "left the merge queue green and was dropped again after {tries} requeues"
BACKOFF_MS = 60_000
RECOVER = "recover"
ALERT_SOURCE = "requeue"
TRANSIENT = "unmergeable without a conflict; the unchanged head is queued again once it reads mergeable and green"
STUCK = "stayed unmergeable after leaving the merge queue"
MOVED = "changed its head after leaving the merge queue"
DEQUEUED = (
    "The swarm took {url} out of the merge queue: GitHub marked it unmergeable without a conflict. It queues the "
    "unchanged head again once it reads mergeable and green, so the task keeps its claim."
)
RECOVERED = "The swarm queued {url} again: its unmergeable state cleared on the unchanged green head."
REQUEUE_FAILED = "The swarm could not queue {url} again for task {task} after a transient unmergeable state: {why}"
GAVE_UP = "The swarm gave up queueing {url} again for task {task}: it stayed unmergeable or not green; reopened."
PROOF = (
    "The swarm merged {url}. This {kind} task stays open for its proof; the next engineer continues it from the "
    "handoff its engineer left."
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
    staged_compatibility.require(url, pull.head, pull.body)
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
    exits: Callable = queue_exit.own_failures
    journal: Callable = queue_record.Journal


def merge_pass(run, rows):
    actions = []
    for task_id, row in rows.items():
        row = dict(row)
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
    if held["kind"] == RECOVER:
        return _recover(run, row, found)
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
    if outcome.startswith(f"pull request {url}, now green"):
        return _requeue(run, row, found)
    red = outcome.startswith(f"pull request {url}, now red")
    return _reopen(run, row, found, HEAD_RED if red else "left the merge queue without merging")


def _requeue(run, row, found):
    task_id, url = row["id"], found["url"]
    try:
        failures = run.exits(url, found["queued_at"])
    except SwarmError as exc:
        save(run.store, run.slug, task_id, found)
        return [f"could not read the merge group runs of {url} for task {task_id}: {exc}"]
    if failures is None:
        return _reopen(run, row, found, UNREAD_EXIT)
    if failures:
        return _reopen(run, row, found, OWN_RED)
    if found.get("requeues", 0) >= QUEUE_TRIES:
        return _reopen(run, row, found, DROPPED_GREEN.format(tries=QUEUE_TRIES))
    actions = _queue(run, row, found)
    if actions == [f"queued {url} for task {task_id}"]:
        found["requeues"] = found.get("requeues", 0) + 1
        save(run.store, run.slug, task_id, found)
        run.ledger.comment(run.slug, task_id, REQUEUED.format(url=url), by="swarm")
    return actions


def _checks(run, row, found, pull):
    outcome = waits.checks_resolution(found["on"], run.github, run.reread)
    if not outcome.startswith(f"checks on {found['url']}"):
        save(run.store, run.slug, row["id"], found)
        return []
    if outcome.startswith(f"checks on {found['url']}, now red"):
        steps = ", ".join(pull.failed)
        return _reopen(run, row, found, f"has red checks: {steps}" if steps else "has red checks")
    return _queue(run, row, found)


def _journal(run, task_id, url):
    return run.journal(url, context={"swarm": run.slug, "task": task_id, "timer": os.getpid(), "by": "merge watcher"})


def _queue(run, row, found):
    task_id, url = row["id"], found["url"]
    try:
        run.queue("queue", url, journal=_journal(run, task_id, url))
    except SwarmError as exc:
        if str(exc).startswith(REFUSALS):
            found["refusals"] = found.get("refusals", 0) + 1
            if found["refusals"] >= QUEUE_TRIES:
                return _reopen(run, row, found, f"could not be queued: {exc}")
        elif not any(hold in str(exc) for hold in HOLDS):
            return _reopen(run, row, found, f"could not be queued: {exc}")
        save(run.store, run.slug, task_id, found)
        return [f"could not queue {url} for task {task_id}: {exc}"]
    found["on"], found["queued_at"] = {"kind": "merge", "target": url}, run.now_ms
    save(run.store, run.slug, task_id, found)
    return [f"queued {url} for task {task_id}"]


def _finish(run, rows, row, found):
    task_id, url = row["id"], found["url"]
    kind = ledger_kinds.kind(row)
    verify = ledger_kinds.after(row) and ledger_kinds.unmet(row)
    if kind not in GATED or verify:
        if not _release(run, row, found, "verify" if verify else ""):
            return []
        run.ledger.comment(run.slug, task_id, PROOF.format(url=url, kind=kind), by="swarm")
        return [f"task {task_id} awaits its proof, {url} merged", *_remove(found)]
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


def _owners(run, task_id):
    from scripts.swarm.runtime import HerdrRuntime
    from scripts.swarm.status import live_agents

    agents = [agent for agent in run.store.agents(run.slug) if agent.task == task_id]
    live = live_agents(agents, HerdrRuntime())
    return [agent for agent in agents if agent.state != "finished" or agent in live]


def _working_owner(run, row, pull):
    owners = _owners(run, row["id"])
    for agent in owners:
        identity = json.dumps(
            [run.slug, row["id"], agent.name, agent.execution_id, agent.generation, pull.head, pull.merge_problem]
        )
        item_id = sha256(identity.encode()).hexdigest()
        try:
            InboxStore(run.store.redis).send(
                "swarm",
                agent.name,
                f"Your pull request {row['pr_url']} {pull.merge_problem}. Restack it onto dev and publish the fix; keep your claim.",
                task=row["id"],
                item_id=item_id,
            )
        except DuplicateItem:
            pass
    return bool(owners)


def _unmergeable(run, row, found, pull):
    if _working_owner(run, row, pull):
        return []
    task_id, url = row["id"], found["url"]
    fresh = run.reread(url)
    if fresh is None or not fresh.merge_problem:
        return []
    if fresh.queued:
        try:
            result = run.queue("dequeue", url, journal=_journal(run, task_id, url))
        except SwarmError as exc:
            return [f"could not dequeue {url} for task {task_id}: {exc}"]
        if result.get("queued") or result.get("state") == "MERGED":
            return [f"dequeue of {url} for task {task_id} awaits confirmation"]
    head = fresh.head or found.get("head")
    if found.get("on") and head and fresh.mergeable != "CONFLICTING":
        tries = found.get("recoveries", 0)
        if tries >= QUEUE_TRIES:
            run.ledger.raise_alert(run.slug, ALERT_SOURCE, GAVE_UP.format(url=url, task=task_id))
            return _reopen(run, row, found, STUCK)
        found["on"] = {"kind": RECOVER, "head": head}
        found["retry_at"], found["recoveries"] = run.now_ms + BACKOFF_MS * 2**tries, tries
        save(run.store, run.slug, task_id, found)
        run.ledger.notice(run.slug, task_id, DEQUEUED.format(url=url))
        return [f"dequeued {url} for task {task_id}: {TRANSIENT}"]
    return _reopen(run, row, found, fresh.merge_problem)


def _recover(run, row, found):
    task_id, url = row["id"], found["url"]
    if run.now_ms < found["retry_at"]:
        return []
    fresh = run.reread(url)
    if fresh is None:
        return []
    if fresh.head and fresh.head != found["on"]["head"]:
        return _reopen(run, row, found, MOVED)
    if fresh.mergeable == "CONFLICTING":
        return _reopen(run, row, found, fresh.merge_problem)
    if fresh.red:
        return _reopen(run, row, found, "has red checks")
    if fresh.merge_problem or not fresh.resolved:
        return _retry(run, row, found, f"{url} for task {task_id} is not yet mergeable and green; it retries later")
    try:
        run.queue("queue", url, journal=_journal(run, task_id, url))
    except SwarmError as exc:
        run.ledger.raise_alert(run.slug, ALERT_SOURCE, REQUEUE_FAILED.format(url=url, task=task_id, why=exc))
        return _retry(run, row, found, f"could not requeue {url} for task {task_id}: {exc}")
    found["on"], found["queued_at"] = {"kind": "merge", "target": url}, run.now_ms
    found["recoveries"] += 1
    save(run.store, run.slug, task_id, found)
    run.ledger.notice(run.slug, task_id, RECOVERED.format(url=url))
    return [f"queued {url} for task {task_id}"]


def _retry(run, row, found, action):
    tries = found["recoveries"] + 1
    if tries >= QUEUE_TRIES:
        run.ledger.raise_alert(run.slug, ALERT_SOURCE, GAVE_UP.format(url=found["url"], task=row["id"]))
        return _reopen(run, row, found, STUCK)
    found["recoveries"], found["retry_at"] = tries, run.now_ms + BACKOFF_MS * 2**tries
    save(run.store, run.slug, row["id"], found)
    return [action]


def _reopen(run, row, found, what):
    task_id, url = row["id"], found["url"]
    if not _release(run, row, found, FIX):
        return []
    run.ledger.comment(run.slug, task_id, REOPENED.format(url=url, what=what), by="swarm")
    return [f"task {task_id} reopened, {url} {what}"]


def _release(run, row, found, wait):
    task_id = row["id"]
    owners = _owners(run, task_id)
    if owners:
        if wait == "verify":
            _proof_wait(run, row, owners)
        return False
    lease_generation = run.store.claim_generation(run.slug, task_id)
    expected_ready = run.store.redis.hget(run.store.key(run.slug, RECORDS), task_id)
    expected = {key: row.get(key, "") for key in ("claimed_by", "pr_url", "awaiting")}
    expected["claim_generation"] = row.get("claim_generation", 0)
    live = run.ledger.update_task(
        run.slug, task_id, {"state": "open", "claimed_by": "", "awaiting": wait}, if_state=("pr",), if_match=expected
    )
    generation = expected["claim_generation"] + (row.get("claimed_by") != "")
    if live.get("state") != "open" or live.get("claimed_by") or live.get("claim_generation", 0) != generation:
        return False
    return run.store.release_ready(run.slug, task_id, found["agent"], lease_generation, expected_ready)


def _proof_wait(run, row, owners):
    until = int((row.get("contract") or {}).get("after_at") or run.now_ms + 60_000)
    if until <= run.now_ms:
        return
    for owner in owners:
        idle.declare_wait(run.store.redis, run.slug, owner.name, until, "post merge proof", run.now_ms)


def _remove(found, *flags):
    top = found["worktree"]
    if not top:
        return []
    done = stack.shell(["bash", str(stack.WT_SCRIPT), "done", Path(top).name, "--repo", top, *flags])
    if done.returncode != 0:
        return [f"could not remove the worktree {top}: {done.stderr.strip()}"]
    return [f"removed the worktree {top}"]
