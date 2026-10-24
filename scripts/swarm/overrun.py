"""Wall time per claimed task: past one limit the master hears of it, past a second the agent is asked to push and
hand off, and an agent still live a grace period after that ask is ended and its task reopened for the next agent.
Declared waits count toward the age; only a wait on the merge queue holds the ask and the end."""

import json
from collections.abc import Callable
from dataclasses import dataclass

from scripts.inbox import exits
from scripts.inbox.store import CLOSED, InboxStore
from scripts.swarm import capacity, idle, outage
from scripts.swarm.health.findings import MINUTE_MS
from scripts.swarm.ledger_events import Mail
from scripts.swarm_ledger import ledger_workspace

REPORT_MINUTES, PARK_MINUTES, GRACE_MINUTES = 90, 150, 15
SENDER, KEY, CAUSE = "swarm", "overrun", "overrun"
HELD = ("claimed", "pr")
FINISHED, STOPPED, MERGE, DONE = "finished", "stopped", "merge", "done"
ASK_CLOSED = "the overrun watch ended the agent it asked"
NO_STEPS = "no steps recorded in progress.md"
REPORT = (
    "Overrun on swarm {slug}: {agent} has run {minutes} minutes on task {task} without finishing it. Last steps: "
    "{steps}. At {park} minutes the swarm asks it to push and hand off; {grace} minutes after that it ends the agent "
    "and reopens the task. Close this item once you have looked."
)
ASK = (
    "Overrun on swarm {slug}: you have run {minutes} minutes on task {task}, past the {park} minute limit. Finish the "
    "step you are on, push any commits with agentihooks swarm {slug} publish, write a Handoff v2 naming what blocks you, "
    "run agentihooks swarm {slug} handoff <doc> and stop; a successor continues the task. Still running in {grace} "
    "minutes, you are ended and the task reopens for the next agent."
)
ENDED = (
    "Overrun on swarm {slug}: {agent} was still running {grace} minutes after the ask to hand off task {task}, "
    "{minutes} minutes in all. The swarm ended it and reopened the task for the next agent, who continues from "
    "its work folder and any pushed branch. Close this item once you have looked."
)
COMMENT = (
    "The overrun watch ended {agent} after {minutes} minutes on this task: it was still running {grace} minutes "
    "after the ask to push and hand off. The task is open again for the next agent."
)


@dataclass(frozen=True)
class Watch:
    store: object
    slug: str
    ledger: object
    live: frozenset
    retire: Callable
    now_ms: int


def overrun_pass(watch, doc):
    key = watch.store.key(watch.slug, KEY)
    marks = watch.store.redis.hgetall(key)
    rows = {task["id"]: task for task in doc.get("tasks") or []}
    mail = Mail(InboxStore(watch.store.redis), watch.store, watch.slug, doc)
    actions, kept = [], {}
    for agent in watch.store.agents(watch.slug):
        if agent.state == FINISHED:
            continue
        mark = json.loads(marks.get(agent.name) or "{}")
        if mark.get("task") != agent.task:
            mark = {}
        if _holding(watch, agent, rows) and not outage.held(watch.store, watch.slug, agent.name):
            actions += _step(watch, mail, agent, mark)
        if mark:
            kept[agent.name] = json.dumps({**mark, "task": agent.task})
    pipe = watch.store.redis.pipeline()
    pipe.delete(key)
    if kept:
        pipe.hset(key, mapping=kept)
    pipe.execute()
    return actions


def _minutes(watch, since):
    return (watch.now_ms - since - outage.paused_ms(watch.slug, since, watch.now_ms)) // MINUTE_MS


def _holding(watch, agent, rows):
    row = rows.get(agent.task) or {}
    return (
        agent.lane in capacity.LANES
        and agent.started_at > 0
        and agent.name in watch.live
        and row.get("state") in HELD
        and row.get("claimed_by") == agent.name
    )


def _queued(watch, agent):
    held = idle.wait(watch.store.redis, watch.slug, agent.name)
    return held is not None and held["until"] > watch.now_ms and (held.get("on") or {}).get("kind") == MERGE


def _step(watch, mail, agent, mark):
    minutes, actions = _minutes(watch, agent.started_at), []
    if minutes >= REPORT_MINUTES and not mark.get("reported"):
        mark["reported"] = watch.now_ms
        actions.append(_report(watch, mail, agent, minutes))
    if _queued(watch, agent):
        return actions
    if minutes >= PARK_MINUTES and "asked" not in mark:
        mark["asked"] = watch.now_ms
        actions.append(_ask(watch, mail, agent, minutes, mark))
    elif "asked" in mark and _minutes(watch, mark["asked"]) >= GRACE_MINUTES:
        return actions + _end(watch, mail, agent, minutes, mark)
    return actions


def _steps(slug, task):
    lines = ledger_workspace.tails(slug, task).get("latest_progress", "").splitlines()
    return "; ".join(line.strip() for line in lines if line.strip()) or NO_STEPS


def _report(watch, mail, agent, minutes):
    address = mail.owner(f"tasks/{agent.task}")
    text = REPORT.format(
        slug=watch.slug,
        agent=agent.name,
        minutes=minutes,
        task=agent.task,
        steps=_steps(watch.slug, agent.task),
        park=PARK_MINUTES,
        grace=GRACE_MINUTES,
    )
    mail.inbox.send(SENDER, address, text, ref=f"{watch.slug}:{KEY}:{agent.task}", task=agent.task)
    return f"overrun: told {address} that {agent.name} has run {minutes} minutes on task {agent.task}"


def _ask(watch, mail, agent, minutes, mark):
    text = ASK.format(slug=watch.slug, minutes=minutes, task=agent.task, park=PARK_MINUTES, grace=GRACE_MINUTES)
    item = mail.inbox.send(SENDER, agent.name, text, ref=f"{watch.slug}:{KEY}:{agent.task}", task=agent.task)
    mark["ask"] = item.id
    return f"overrun: asked {agent.name} to push and hand off task {agent.task} after {minutes} minutes"


def _end(watch, mail, agent, minutes, mark):
    tid = agent.task
    fresh = watch.ledger.task(watch.slug, tid) or {}
    if fresh.get("state") not in HELD or fresh.get("claimed_by") != agent.name:
        return []
    if not watch.retire(agent):
        return [f"overrun: could not end {agent.name} on task {tid}, retrying next tick"]
    reopened = watch.ledger.update_task(
        watch.slug, tid, {"state": "open", "claimed_by": "", "awaiting": ""}, if_state=HELD
    )
    if reopened.get("state") != "open" or reopened.get("claimed_by"):
        return [f"overrun: ended {agent.name} but could not reopen task {tid}, the stall watch reopens it"]
    watch.store.mark_restart(watch.slug, tid, CAUSE)
    watch.store.release(watch.slug, tid, agent.name)
    watch.store.drop_agent(watch.slug, agent.name)
    if mail.inbox.get(mark["ask"]).state not in CLOSED:
        mail.inbox.close(mark["ask"], SENDER, DONE, ASK_CLOSED)
    exits.settle(mail.inbox, agent.name, agent.seat, STOPPED)
    said = {"slug": watch.slug, "agent": agent.name, "minutes": minutes, "task": tid, "grace": GRACE_MINUTES}
    watch.ledger.comment(watch.slug, tid, COMMENT.format(**said), by=SENDER)
    mail.inbox.send(SENDER, mail.owner(f"tasks/{tid}"), ENDED.format(**said), ref=f"{watch.slug}:{KEY}:{tid}", task=tid)
    mark.clear()
    return [f"overrun: ended {agent.name} on task {tid} after {minutes} minutes and reopened the task"]
