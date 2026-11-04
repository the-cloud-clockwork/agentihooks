"""Wall time per claimed task: past REPORT_MINUTES the master hears one inbox report. Only the context limit
triggers a handoff; wall time on a task never asks an agent to hand off and never retires or reopens anything."""

import json
from dataclasses import dataclass

from scripts.inbox.store import InboxStore
from scripts.swarm import capacity, outage
from scripts.swarm.health.findings import MINUTE_MS
from scripts.swarm.ledger_events import Mail
from scripts.swarm_ledger import ledger_workspace

REPORT_MINUTES = 90
SENDER, KEY = "swarm", "overrun"
HELD = ("claimed", "pr")
FINISHED = "finished"
NO_STEPS = "no steps recorded in progress.md"
REPORT = (
    "Overrun on swarm {slug}: {agent} has run {minutes} minutes on task {task} without finishing it. Last steps: "
    "{steps}. Close this item once you have looked."
)


@dataclass(frozen=True)
class Watch:
    store: object
    slug: str
    live: frozenset
    now_ms: int


def overrun_pass(watch: "Watch", doc: dict) -> list[str]:
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


def _minutes(watch: "Watch", since: int) -> int:
    return (watch.now_ms - since - outage.paused_ms(watch.slug, since, watch.now_ms)) // MINUTE_MS


def _holding(watch: "Watch", agent: object, rows: dict) -> bool:
    row = rows.get(agent.task) or {}
    return (
        agent.lane in capacity.LANES
        and agent.started_at > 0
        and agent.name in watch.live
        and row.get("state") in HELD
        and row.get("claimed_by") == agent.name
    )


def _step(watch: "Watch", mail: Mail, agent: object, mark: dict) -> list[str]:
    minutes = _minutes(watch, agent.started_at)
    if minutes >= REPORT_MINUTES and not mark.get("reported"):
        mark["reported"] = watch.now_ms
        return [_report(watch, mail, agent, minutes)]
    return []


def _steps(slug: str, task: str) -> str:
    lines = ledger_workspace.tails(slug, task).get("latest_progress", "").splitlines()
    return "; ".join(line.strip() for line in lines if line.strip()) or NO_STEPS


def _report(watch: "Watch", mail: Mail, agent: object, minutes: int) -> str:
    address = mail.owner(f"tasks/{agent.task}")
    text = REPORT.format(
        slug=watch.slug, agent=agent.name, minutes=minutes, task=agent.task, steps=_steps(watch.slug, agent.task)
    )
    mail.inbox.send(SENDER, address, text, ref=f"{watch.slug}:{KEY}:{agent.task}", task=agent.task)
    return f"overrun: told {address} that {agent.name} has run {minutes} minutes on task {agent.task}"
