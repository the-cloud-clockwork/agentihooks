"""Checked waits: a swarm agent waits on its pull request's checks, a reply to an inbox item or another task.

`swarm wait --on KIND TARGET` records the wait; the minute tick ends it when the thing resolves and tells the agent
through its inbox, which the wake ladder delivers. A bare minutes wait, checked by nothing, lasts at most an hour.
"""

import json
import re

from scripts.inbox.store import CLOSED, InboxError
from scripts.swarm import idle, mutation_wait
from scripts.swarm.store import SwarmError

KINDS = ("checks", "merge", "reply", "task")
BARE_MAX_MINUTES = 60
CHECKED_MINUTES = 12 * 60
FRESH_MS = 5 * 60_000
TASK_ENDS = ("done", "blocked")
SENDER = "swarm"
PULL_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
NOTICE_RE = re.compile(r"Your wait on .+ has ended\. Pick task (\S+) back up:")


def on(kind, target):
    if kind not in KINDS:
        raise SwarmError(f"wait on one of: {', '.join(KINDS)}")
    return {"kind": kind, "target": target}


def checks_resolution(held, github):
    target = held["target"]
    pull = github(target)
    if pull is None:
        return ""
    if pull.state != "OPEN":
        return f"pull request {target}, now {pull.state.lower()}"
    if not pull.head:
        return ""
    if held.get("head") != pull.head:
        held["head"] = pull.head
        return ""
    if not pull.resolved:
        return ""
    current = github(target)
    if current is None or not current.head:
        return ""
    if current.head != pull.head:
        held["head"] = current.head
        return ""
    if not current.resolved:
        return ""
    outcome = f"checks on {target}, now {'red' if current.red else 'green'}"
    return f"{outcome}; {current.unpassed_gate} never passed" if current.unpassed_gate else outcome


def _save_wait(redis, slug, name, previous, held, outcome, now_ms):
    key = idle.key(slug, "wait", name)

    def update(pipe):
        if pipe.get(key) != previous:
            return False
        pipe.multi()
        if outcome:
            pipe.delete(key)
            pipe.set(idle.key(slug, "waited", name), now_ms, ex=idle.BEAT_TTL_S)
        else:
            pipe.set(key, json.dumps(held), keepttl=True)
        return True

    return redis.transaction(update, key, value_from_callable=True)


def target_problem(kind, target, mine, rows, get):
    """Why the target cannot be waited on, or '' when the tick can check it."""
    if kind in ("checks", "merge"):
        return "" if PULL_URL.fullmatch(target) else f"wait on {kind} needs a pull request url, not {target}"
    if kind == "mutation":
        return "" if mutation_wait.RUN_URL.fullmatch(target) else "wait on mutation needs an Actions run url"
    if kind == "task":
        if target == mine:
            return f"task {target} is your own task"
        if target not in rows:
            return f"no task {target} on the ledger"
        state = rows[target].get("state")
        return f"task {target} is already {state}" if state in TASK_ENDS else ""
    try:
        get(target)
    except InboxError:
        return f"no inbox item {target}"
    return ""


def merge_resolution(held, github, reread, fresh):
    target = held["target"]
    pull = github(target)
    if pull is not None and pull.state == "OPEN" and not pull.queued:
        pull = reread(target)
    if pull is None:
        return ""
    if pull.state == "MERGED":
        return f"pull request {target}, now merged"
    if pull.state == "OPEN":
        if pull.queued:
            held["queued"] = True
            return ""
        if not pull.resolved or (fresh and not held.get("queued")):
            return ""
    return f"pull request {target}, now red; left the merge queue without merging; fix it and queue it again"


def resolution(held, rows, inbox, github, reread, fresh):
    """What ended the wait, in plain words, or '' while it still holds."""
    kind, target = held["kind"], held["target"]
    if kind == "mutation":
        return mutation_wait.resolution(held)
    if kind == "checks":
        return checks_resolution(held, github)
    if kind == "merge":
        return merge_resolution(held, github, reread, fresh)
    if kind == "task":
        if target not in rows:
            return f"task {target}, gone from the ledger"
        state = rows[target].get("state")
        return f"task {target}, now {state}" if state in TASK_ENDS else ""
    try:
        item = inbox.get(target)
    except InboxError:
        return f"message {target}, gone from the inbox"
    return f"message {target}, now {item.state}" if item.state in CLOSED else ""


def end_pass(store, slug, rows, inbox, github, now_ms, reread):
    ended = []
    for agent in store.agents(slug):
        held = idle.wait(store.redis, slug, agent.name)
        if agent.state == "finished" or not (held and held.get("on")):
            continue
        previous = json.dumps(held)
        outcome = resolution(held["on"], rows, inbox, github, reread, now_ms - held["at"] < FRESH_MS)
        if (outcome or json.dumps(held) != previous) and not _save_wait(
            store.redis, slug, agent.name, previous, held, outcome, now_ms
        ):
            continue
        if not outcome:
            continue
        text = (
            f"Your wait on {outcome} has ended.{_pick_up(agent.task)} agentihooks swarm {slug} done, block, "
            "or wait on the next thing."
        )
        inbox.send(SENDER, agent.seat or agent.name, text)
        ended.append(f"ended the wait of {agent.name}: {outcome}")
    return ended


def _pick_up(task):
    return f" Pick task {task} back up:"


def notice_task(item):
    """The task a wait ended notice asks its receiver to pick back up, '' for any other item."""
    found = NOTICE_RE.match(item.text) if item.sender == SENDER else None
    return found.group(1) if found else ""


def settle_notices(inbox, agent, action):
    """Close the open wait ended notices for the agent's task, naming the action it recorded on that task."""
    for address in dict.fromkeys(filter(None, (agent.seat, agent.name))):
        for item in inbox.inbox(address):
            if item.sender != SENDER or item.state in CLOSED or _pick_up(agent.task) not in item.text:
                continue
            inbox.close(item.id, SENDER, "done", f"{agent.name} recorded {action} on task {agent.task}")
