"""Checked waits: a swarm agent waits on its pull request's checks, a reply to an inbox item or another task.

`swarm wait --on KIND TARGET` records the wait; the minute tick ends it when the thing resolves and tells the agent
through its inbox, which the wake ladder delivers. A bare minutes wait, checked by nothing, lasts at most an hour.
"""

import re

from scripts.inbox.store import CLOSED, InboxError
from scripts.swarm import idle
from scripts.swarm.store import SwarmError

KINDS = ("checks", "reply", "task")
BARE_MAX_MINUTES = 60
CHECKED_MINUTES = 12 * 60
TASK_ENDS = ("done", "blocked")
SENDER = "swarm"
PULL_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")


def on(kind, target):
    if kind not in KINDS:
        raise SwarmError(f"wait on one of: {', '.join(KINDS)}")
    return {"kind": kind, "target": target}


def target_problem(kind, target, mine, rows, get):
    """Why the target cannot be waited on, or '' when the tick can check it."""
    if kind == "checks":
        return "" if PULL_URL.fullmatch(target) else f"wait on checks needs a pull request url, not {target}"
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


def resolution(held, rows, inbox, github):
    """What ended the wait, in plain words, or '' while it still holds."""
    kind, target = held["kind"], held["target"]
    if kind == "checks":
        pull = github(target)
        if pull is None:
            return ""
        if pull.state != "OPEN":
            return f"pull request {target}, now {pull.state.lower()}"
        return f"checks on {target}, now {'red' if pull.red else 'green'}" if pull.resolved else ""
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


def end_pass(store, slug, rows, inbox, github):
    ended = []
    for agent in store.agents(slug):
        held = idle.wait(store.redis, slug, agent.name)
        if agent.state == "finished" or not (held and held.get("on")):
            continue
        outcome = resolution(held["on"], rows, inbox, github)
        if not outcome:
            continue
        idle.end_wait(store.redis, slug, agent.name)
        text = (
            f"Your wait on {outcome} has ended.{_pick_up(agent.task)} agentihooks swarm {slug} done, block, "
            "or wait on the next thing."
        )
        inbox.send(SENDER, agent.seat or agent.name, text)
        ended.append(f"ended the wait of {agent.name}: {outcome}")
    return ended


def _pick_up(task):
    return f" Pick task {task} back up:"


def settle_notices(inbox, agent, action):
    """Close the open wait ended notices for the agent's task, naming the action it recorded on that task."""
    for address in dict.fromkeys(filter(None, (agent.seat, agent.name))):
        for item in inbox.inbox(address):
            if item.sender != SENDER or item.state in CLOSED or _pick_up(agent.task) not in item.text:
                continue
            try:
                inbox.close(item.id, SENDER, "done", f"{agent.name} recorded {action} on task {agent.task}")
            except InboxError:
                continue
