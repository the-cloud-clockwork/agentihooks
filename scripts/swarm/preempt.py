"""Operator priority preemption: an urgent or focused task still claimable a tick after its lane filled asks the
lowest ranked holder that is neither urgent nor focused, through its inbox, to push and hand off. Nothing is frozen;
the freed seat goes to the waiting task because the spawn order puts urgent work first."""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass

from scripts.inbox.store import DuplicateItem, InboxStore
from scripts.swarm import dispatcher, freeze
from scripts.swarm_ledger import ledger_rank
from scripts.swarm_ledger.repository import hierarchy

SENDER = "swarm"
REF = "preempt"
WAITING = "preempt-waiting"
ASKED = "preempt-asked"
ANSWER_MS = 10 * 60_000
URGENT = "urgent"
YIELDED = 0.5
TEXT = (
    "Operator priority on swarm {slug}: task {waiting} is urgent or focused and waits for a seat while every {lane} "
    "seat is busy, and your task {task} ranks lower. Push your branch now, write a Handoff v2 with the handoff skill "
    "naming where you stopped, run agentihooks swarm {slug} handoff <doc> --reason operator, and stop. Your task "
    "reopens for a later seat and nothing is frozen. Close this message with agentihooks msg close and its id, done."
)


@dataclass(frozen=True)
class Seats:
    rows: dict
    holds: Callable
    ready: Callable


def run(slug: str, config, store, doc: dict, now_ms: int, seats: Seats) -> list[str]:
    from scripts.swarm import capacity

    caps = capacity.caps(capacity.read(store, slug), config)
    dispatched, agents, full = yielding(store, slug, doc, dispatcher.claim_key(config, doc)), store.agents(slug), {}
    for lane, cap in caps.items():
        holders = [a for a in agents if a.lane == lane and seats.holds(a)]
        if cap and len(holders) >= cap:
            full[lane] = (seats.ready(slug, store, seats.rows, doc, lane, dispatched), holders)
    return step(slug, store, seats.rows, doc, full, now_ms)


def yielding(store, slug: str, doc: dict, dispatched):
    """A handoff successor of other work sorts just behind urgent work while an urgent task is open, whatever its band."""
    if dispatched is None or not any(t.get("rank") == URGENT and t.get("state") == "open" for t in doc["tasks"]):
        return dispatched

    def key(task):
        band, position = dispatched(task)
        if task.get("rank") != URGENT and store.handoff(slug, task["id"]):
            return max(band, YIELDED), position
        return band, position

    return key


def preferred(task: dict, doc: dict, graph: dict) -> bool:
    if not task.get("id") or task.get("rank") == URGENT:
        return True
    chain = freeze.ancestry(task, graph)
    return any(r["verb"] == "focus" and freeze.covers(r["target"], task, chain) for r in doc.get("freezes") or [])


def step(slug: str, store, rows: dict, doc: dict, full: dict, now_ms: int) -> list[str]:
    """full maps each lane whose every seat is busy to its claimable tasks in claim order and its live holders."""
    graph, redis = hierarchy.project(doc)[0], store.redis
    seen, asked = redis.smembers(store.key(slug, WAITING)), redis.hgetall(store.key(slug, ASKED))
    holding = {a.name for _, holders in full.values() for a in holders}
    asked = {name: entry for name, entry in asked.items() if name in holding}
    waiting, actions = set(), []
    for lane, (ready, holders) in full.items():
        for task in (t for t in ready if preferred(t, doc, graph)):
            waiting.add(task["id"])
            if task["id"] not in seen or _pending(asked, task["id"], now_ms):
                continue
            victim = _victim([a for a in holders if a.name not in asked], rows, doc, graph)
            if victim is not None:
                asked[victim.name] = f"{task['id']}:{now_ms}"
                actions += _ask(InboxStore(redis), slug, lane, victim, task["id"])
    redis.delete(store.key(slug, WAITING))
    if waiting:
        redis.sadd(store.key(slug, WAITING), *waiting)
    _replace(redis, store.key(slug, ASKED), asked)
    return actions


def _pending(asked: dict, task_id: str, now_ms: int) -> bool:
    return any(
        target == task_id and now_ms - int(at) < ANSWER_MS
        for target, _, at in (entry.rpartition(":") for entry in asked.values())
    )


def _victim(holders, rows, doc, graph):
    lower = [a for a in holders if not preferred(rows.get(a.task, {}), doc, graph)]
    return max(lower, key=lambda a: (ledger_rank.order(rows[a.task]), a.started_at), default=None)


def _ask(inbox, slug, lane, victim, waiting):
    item_id = hashlib.sha256(f"{REF}:{slug}:{victim.name}:{waiting}".encode()).hexdigest()[:12]
    text = TEXT.format(slug=slug, waiting=waiting, lane=lane, task=victim.task)
    try:
        item = inbox.send(SENDER, victim.name, text, ref=f"{REF}:{item_id}", task=victim.task, item_id=item_id)
    except DuplicateItem:
        return []
    return [f"asked {victim.name} to hand off task {victim.task} for urgent task {waiting} as message {item.id}"]


def _replace(redis, key, mapping):
    redis.delete(key)
    if mapping:
        redis.hset(key, mapping=mapping)
