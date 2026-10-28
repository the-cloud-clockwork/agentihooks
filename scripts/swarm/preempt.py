"""Operator priority preemption: an urgent or focused task still claimable a tick after its lane filled asks the
lowest ranked holder that is neither urgent nor focused, through its inbox, to push and hand off. Nothing is frozen;
the freed seat goes to the waiting task because the spawn order puts urgent work first."""

import json
from collections.abc import Callable
from dataclasses import dataclass

from scripts.inbox.store import InboxStore
from scripts.swarm import dispatcher, freeze
from scripts.swarm_ledger import ledger_rank
from scripts.swarm_ledger.repository import hierarchy

SENDER = "swarm"
REF = "preempt"
WAITING = "preempt-waiting"
ASKED = "preempt-asked"
VICTIMS = "preempt-victims"
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
    ended: Callable
    owner: Callable
    ready: Callable
    placement: Callable

    def holds(self, agent) -> bool:
        return not self.ended(agent, self.rows) and self.owner(agent, self.rows.get(agent.task, {}))


def _places(lane: str, task: dict) -> str:
    return ""


def probe(runtime, lane: str, task: dict) -> str:
    from scripts.swarm.profile_choice import ProfileUnresolved
    from scripts.swarm.seat_spawn import placed_elsewhere

    try:
        return placed_elsewhere(runtime, lane, task)
    except ProfileUnresolved as exc:
        return f"profile unresolved: {exc}"


def run(slug: str, config, store, doc: dict, now_ms: int, seats: Seats) -> list[str]:
    from scripts.swarm import capacity

    caps = capacity.caps(capacity.read(store, slug), config)
    dispatched, agents, full = yielding(store, slug, doc, dispatcher.claim_key(config, doc)), store.agents(slug), {}
    for lane, cap in caps.items():
        holders = [a for a in agents if a.lane == lane and seats.holds(a)]
        if cap and len(holders) >= cap:
            full[lane] = (seats.ready(slug, store, seats.rows, doc, lane, dispatched), holders)
    return step(slug, store, seats.rows, doc, full, now_ms, seats.placement)


def yielding(store, slug: str, doc: dict, dispatched):
    """A guarded handoff successor of other work sorts just behind urgent work while an urgent task is open."""
    if dispatched is None or not any(t.get("rank") == URGENT and t.get("state") == "open" for t in doc["tasks"]):
        return dispatched

    handoffs = store.handoff_tasks(slug, [task["id"] for task in doc["tasks"]])

    def key(task):
        band, position = dispatched(task)
        if task.get("rank") != URGENT and task["id"] in handoffs:
            return max(band, YIELDED), position
        return band, position

    return key


def preferred(task: dict, doc: dict, graph: dict) -> bool:
    if not task.get("id") or task.get("rank") == URGENT:
        return True
    chain = freeze.ancestry(task, graph)
    return any(r["verb"] == "focus" and freeze.covers(r["target"], task, chain) for r in doc.get("freezes") or [])


def step(slug: str, store, rows: dict, doc: dict, full: dict, now_ms: int, probe: Callable = _places) -> list[str]:
    """full maps each lane whose every seat is busy to its claimable tasks in claim order and its live holders;
    probe(lane, task) returns why placement refuses the task now, empty when it would place."""
    from scripts.hive import placement_waits

    graph, redis = hierarchy.project(doc)[0], store.redis
    seen, asked = redis.smembers(store.key(slug, WAITING)), redis.hgetall(store.key(slug, ASKED))
    refused = placement_waits.read(store, slug)
    holding = {a.name for _, holders in full.values() for a in holders}
    asked = {name: entry for name, entry in asked.items() if name in holding}
    waiting, actions = set(), []
    for lane, (ready, holders) in full.items():
        for task in (t for t in ready if preferred(t, doc, graph)):
            waiting.add(task["id"])
            if task["id"] not in seen or _pending(asked, task["id"], now_ms):
                continue
            if _refused(refused.get(task["id"]), lane, now_ms):
                continue
            victim = _victim([a for a in holders if a.name not in asked], rows, doc, graph)
            if victim is not None and not probe(lane, task):
                asked[victim.name] = json.dumps([task["id"], now_ms])
                redis.hset(store.key(slug, ASKED), victim.name, asked[victim.name])
                redis.hset(store.key(slug, VICTIMS), victim.name, task["id"])
                actions += _ask(InboxStore(redis), slug, lane, victim, task["id"])
    redis.delete(store.key(slug, WAITING))
    if waiting:
        redis.sadd(store.key(slug, WAITING), *waiting)
    _replace(redis, store.key(slug, ASKED), asked)
    return actions


def _pending(asked: dict, task_id: str, now_ms: int) -> bool:
    return any(target == task_id and now_ms - at < ANSWER_MS for target, at in map(json.loads, asked.values()))


def _refused(wait, lane, now_ms):
    from scripts.hive import placement_waits

    return wait is not None and wait["lane"] == lane and now_ms - wait["seen_at"] < placement_waits.WAIT_MS


def take_victim(store, slug: str, name: str) -> bool:
    return bool(store.redis.hdel(store.key(slug, VICTIMS), name))


def _victim(holders, rows, doc, graph):
    lower = [a for a in holders if not preferred(rows.get(a.task, {}), doc, graph)]
    return max(lower, key=lambda a: (ledger_rank.order(rows[a.task]), a.started_at), default=None)


def _ask(inbox, slug, lane, victim, waiting):
    text = TEXT.format(slug=slug, waiting=waiting, lane=lane, task=victim.task)
    item = inbox.send(SENDER, victim.name, text, ref=f"{REF}:{waiting}", task=victim.task)
    return [f"asked {victim.name} to hand off task {victim.task} for urgent task {waiting} as message {item.id}"]


def _replace(redis, key, mapping):
    redis.delete(key)
    if mapping:
        redis.hset(key, mapping=mapping)
