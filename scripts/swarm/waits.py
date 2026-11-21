"""Checked waits: a swarm agent waits on its pull request's checks, a reply to an inbox item or another task.

`swarm wait --on KIND TARGET` records the wait; the minute tick ends it when the thing resolves and tells the agent
through its inbox, which the wake ladder delivers. A bare minutes wait, checked by nothing, lasts at most an hour.
"""

import json
import re
import subprocess
import sys
from collections.abc import Callable
from typing import Any

from scripts.gates import intent
from scripts.gates.verdicts import Verdicts
from scripts.hive import scope
from scripts.inbox.store import CLOSED, Gone, InboxError, InboxStore
from scripts.swarm import idle, lab_wait, merge_queue, run_wait, timing
from scripts.swarm.ledger_events import PullRequest
from scripts.swarm.store import SwarmError

KINDS = ("checks", "merge", "reply", "task", "intent", "lab")
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
    if not current.red and not timing.timed(required_green, target, current.head):
        return ""
    outcome = f"checks on {target}, now {'red' if current.red else 'green'}"
    if current.red and current.failed:
        outcome += f"; failed: {', '.join(current.failed)}"
    return f"{outcome}; {current.unpassed_gate} never passed" if current.unpassed_gate else outcome


def required_green(target: str, head: str) -> bool:
    """Whether GitHub reads every required check of ``target`` passed on ``head``; logs the error a failed read holds on."""
    try:
        pull = merge_queue.required_view(target, subprocess.run)
    except (SwarmError, KeyError, TypeError) as exc:
        print(f"required checks of {target} unread: {exc}", file=sys.stderr)
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


def target_problem(
    kind: str, target: str, mine: str, rows: dict[str, dict[str, Any]], get: Callable[[str], object] | None
) -> str:
    """Why the target cannot be waited on, or '' when the tick can check it."""
    if kind == "checks":
        known = PULL_URL.fullmatch(target) or run_wait.RUN_URL.fullmatch(target)
        return "" if known else f"wait on checks needs a pull request url or an Actions run url, not {target}"
    if kind == "merge":
        return "" if PULL_URL.fullmatch(target) else f"wait on merge needs a pull request url, not {target}"
    if kind == "lab":
        known = lab_wait.CHECK_URL.fullmatch(target) or lab_wait.GRADE_URL.fullmatch(target)
        return "" if known else f"wait on lab needs a check run url or a lab grade url, not {target}"
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


def resolution(
    held: dict[str, Any],
    rows: dict[str, dict[str, Any]],
    inbox: InboxStore | None,
    github: Callable[[str], PullRequest | None],
    reread: Callable[[str], PullRequest | None],
    fresh: bool,
    verdicts: Verdicts | None = None,
) -> str:
    """What ended the wait, in plain words, or '' while it still holds."""
    kind, target = held["kind"], held["target"]
    if kind not in KINDS:
        return f"{kind} {target}, now ended; wait on one of: {', '.join(KINDS)}"
    if kind == "intent":
        return intent_resolution(held, verdicts.read(held["task"]), github)
    if kind == "lab":
        return lab_wait.resolution(target)
    if kind == "checks" and run_wait.RUN_URL.fullmatch(target):
        return run_wait.run_resolution(target)
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
        item = inbox.locate(target)
    except Gone:
        return "" if scope.member() else f"message {target}, gone from the inbox"
    return f"message {target}, now {item.state}" if item.state in CLOSED else ""


def end_pass(
    store: Any, slug: str, rows: dict[str, Any], inbox: Any, github: Any, now_ms: int, reread: Any
) -> list[str]:
    """End each checked wait whose target resolved, telling its holder, then record ``now_ms``, the time the pass
    ran, as the time of the last completed pass, which the idle count trusts."""
    from scripts.swarm import pr_wait

    ended = timing.timed(pr_wait.enforce_pass, store, slug, rows, github, now_ms)
    verdicts = Verdicts(slug, intent.NAME)
    for agent in store.agents(slug):
        try:
            held = idle.wait(store.redis, slug, agent.name)
            if agent.state == "finished" or not (held and held.get("on")):
                continue
            previous = json.dumps(held)
            with timing.acting(agent.name):
                outcome = timing.timed(
                    resolution, held["on"], rows, inbox, github, reread, now_ms - held["at"] < FRESH_MS, verdicts
                )
                outcome = (
                    ""
                    if outcome
                    and agent.task in rows
                    and timing.timed(dependency, held["on"], rows[agent.task], rows, reread)
                    else outcome
                )
                if (outcome or json.dumps(held) != previous) and not timing.timed(
                    _save_wait, store.redis, slug, agent.name, previous, held, outcome, now_ms
                ):
                    continue
                if not outcome:
                    continue
                text = (
                    f"Your wait on {outcome} has ended.{_pick_up(agent.task)} agentihooks swarm {slug} done, block, "
                    "or wait on the next thing."
                )
                timing.timed(inbox.send, SENDER, agent.seat or agent.name, text)
            ended.append(f"ended the wait of {agent.name}: {outcome}")
        except Exception as exc:
            print(f"wait of {agent.name} failed: {exc}", file=sys.stderr)
    mark_ended(store, slug, now_ms)
    return ended


def mark_ended(store: Any, slug: str, now_ms: int) -> None:
    """Record ``now_ms``, the time a completed waits end pass of swarm ``slug`` ran, as the last completed pass."""
    store.redis.set(_ended_key(slug), now_ms)


def ended_at(store: Any, slug: str) -> int | None:
    """Return the time the last completed waits end pass of swarm ``slug`` ran, in epoch milliseconds, or None if no
    pass completed."""
    found = store.redis.get(_ended_key(slug))
    return None if found is None else int(found)


def _ended_key(slug: str) -> str:
    """Return the key holding the time the last completed waits end pass of swarm ``slug`` ran."""
    return idle.key(slug, "waits", "ended")


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
                world.settle(item.id, SENDER, "done", f"{agent.name} recorded {action} on task {agent.task}")
