"""Checked waits: a swarm agent waits on its pull request's checks, a reply to an inbox item or another task.

`swarm wait --on KIND TARGET` records the wait; the minute tick ends it when the thing resolves and tells the agent
through its inbox, which the wake ladder delivers. A bare minutes wait, checked by nothing, lasts at most an hour.
"""

import json
import re
import subprocess
from collections.abc import Callable

from scripts.gates import intent
from scripts.gates.verdicts import Verdicts
from scripts.hive import scope
from scripts.inbox.store import CLOSED, InboxError
from scripts.swarm import idle, merge_queue, mutation_wait
from scripts.swarm.store import SwarmError

KINDS = ("checks", "merge", "reply", "task", "mutation", "intent")
BARE_MAX_MINUTES = 60
CHECKED_MINUTES = 12 * 60
FINISH_TURN = (
    "finish the current turn now unless you have independent work; do not sleep or poll, "
    "the swarm tick ends this wait and sends your next prompt"
)
FRESH_MS = 5 * 60_000
TASK_ENDS = ("done", "blocked")
SENDER = "swarm"
PULL_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
NOTICE_RE = re.compile(r"Your wait on .+ has ended\. Pick task (\S+) back up:")


def dependency(held: dict, task: dict, rows: dict, github: Callable) -> bool:
    kind, target = held.get("kind"), held.get("target")
    if kind == "task":
        return target in rows and target != task.get("id") and rows[target].get("state") not in TASK_ENDS
    if kind == "merge" and target and target != task.get("pr_url"):
        pull = github(target)
        return pull is None or pull.state == "OPEN"
    return False


def on(kind, target, task=""):
    if kind not in KINDS:
        raise SwarmError(f"wait on one of: {', '.join(KINDS)}")
    return {"kind": kind, "target": target, **({"task": task} if kind == "intent" else {})}


def checks_resolution(held, github, reread=None):
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
    if not pull.resolved or (pull.unpassed_gate and not pull.failed):
        return ""
    current = (reread or github)(target)
    if current is None or not current.head:
        return ""
    if current.head != pull.head:
        held["head"] = current.head
        return ""
    if not current.resolved or (current.unpassed_gate and not current.failed):
        return ""
    if not current.red and not required_green(target, current.head):
        return ""
    outcome = f"checks on {target}, now {'red' if current.red else 'green'}"
    if current.red and current.failed:
        outcome += f"; failed: {', '.join(current.failed)}"
    return f"{outcome}; {current.unpassed_gate} never passed" if current.unpassed_gate else outcome


def required_green(target, head):
    try:
        pull = merge_queue.required_view(target, subprocess.run)
    except (SwarmError, KeyError, TypeError):
        return False
    return pull is not None and pull.head == head and pull.resolved and not pull.red


def _save_wait(redis, slug, name, previous, held, outcome, now_ms):
    prefix = scope.readings(redis, slug, name)
    key = idle.key(slug, "wait", name, prefix)

    def update(pipe):
        if pipe.get(key) != previous:
            return False
        pipe.multi()
        if outcome:
            idle.remember_wait(pipe, slug, name, json.loads(previous), now_ms, prefix)
            pipe.delete(key)
            pipe.set(idle.key(slug, "waited", name, prefix), now_ms, ex=idle.BEAT_TTL_S)
            pipe.set(idle.key(slug, "woke", name, prefix), now_ms, ex=idle.BEAT_TTL_S)
        else:
            pipe.set(key, json.dumps(held), keepttl=True)
        return True

    return redis.transaction(update, key, value_from_callable=True)


def target_problem(kind, target, mine, rows, get):
    """Why the target cannot be waited on, or '' when the tick can check it."""
    if kind == "checks":
        known = PULL_URL.fullmatch(target) or mutation_wait.RUN_URL.fullmatch(target)
        return "" if known else f"wait on checks needs a pull request url or an Actions run url, not {target}"
    if kind == "merge":
        return "" if PULL_URL.fullmatch(target) else f"wait on merge needs a pull request url, not {target}"
    if kind == "mutation":
        return "" if mutation_wait.RUN_URL.fullmatch(target) else "wait on mutation needs an Actions run url"
    if kind == "intent":
        own = rows.get(mine, {}).get("pr_url")
        return (
            ""
            if target and target == own
            else f"wait on intent needs the pull request of your task {mine}, not {target}"
        )
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


def named_tasks(kind, target, mine, read):
    """By id, the one task a wait of this kind checks, read with read(task id); no read for any other kind."""
    wanted = {"intent": mine, "task": target}.get(kind)
    found = read(wanted) if wanted else None
    return {wanted: found} if found else {}


def merge_resolution(held, github, reread, fresh):
    target = held["target"]
    pull = github(target)
    if pull is not None and pull.state == "OPEN" and not pull.queued:
        pull = reread(target)
    if pull is None:
        return ""
    if pull.state == "MERGED":
        return f"pull request {target}, now merged"
    if pull.state == "CLOSED":
        return f"pull request {target}, now closed unmerged; reopen it or record why it was closed"
    if pull.queued:
        held["queued"] = True
        return ""
    if not pull.resolved or (fresh and not held.get("queued")):
        return ""
    if pull.red:
        failed = ", ".join(pull.failed) or pull.unpassed_gate or "a required check"
        return f"pull request {target}, now red; failed: {failed}; fix the failing checks and queue it again"
    return f"pull request {target}, now green; dropped from the merge queue; queue it again"


def intent_resolution(held, record, github):
    if not record or record["verdict"] == intent.PENDING:
        return ""
    if record.get("head"):
        pull = github(held["target"])
        if pull is None or pull.head != record["head"]:
            return ""
    return f"the intent verdict on {held['target']}, now {record['verdict']}: {record['reason']}"


def resolution(held, rows, inbox, github, reread, fresh, verdicts=None):
    """What ended the wait, in plain words, or '' while it still holds."""
    kind, target = held["kind"], held["target"]
    if kind == "mutation":
        return mutation_wait.resolution(held)
    if kind == "intent":
        return intent_resolution(held, verdicts.read(held["task"]), github)
    if kind == "checks" and mutation_wait.RUN_URL.fullmatch(target):
        return mutation_wait.run_resolution(target)
    if kind == "checks":
        return checks_resolution(held, github, reread)
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
    ended, verdicts = [], Verdicts(slug, intent.NAME)
    for agent in store.agents(slug):
        held = idle.wait(store.redis, slug, agent.name)
        if agent.state == "finished" or not (held and held.get("on")):
            continue
        previous = json.dumps(held)
        outcome = resolution(held["on"], rows, inbox, github, reread, now_ms - held["at"] < FRESH_MS, verdicts)
        if outcome and agent.task in rows and dependency(held["on"], rows[agent.task], rows, reread):
            outcome = ""
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
    for world in inbox.worlds():
        for address in dict.fromkeys(filter(None, (agent.seat, agent.name))):
            for item in world.inbox(address):
                if item.sender != SENDER or item.state in CLOSED or _pick_up(agent.task) not in item.text:
                    continue
                world.close(item.id, SENDER, "done", f"{agent.name} recorded {action} on task {agent.task}")
