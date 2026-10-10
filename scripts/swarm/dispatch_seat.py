"""The dispatcher's own LLM seat, `dispatcher@<slug>`, woken only on triggers the deterministic passes could not settle.

At full autonomy a trigger wakes the live seat through its inbox or, unless the swarm is paused, spawns it through the
seat spawn helper; below full, once every trigger closes, or once the seat left the ledger, the seat is marked finished
and the reap pass retires it. Triggers that a departed seat had received spawn no new seat; a new trigger spawns one
primed with every open trigger. A trigger is a priority the sweep left unresolved for fifteen minutes, a bottleneck no
lane rule covers held for the lane split's ticks, or a red dev holding blocked tasks, where a freeze or focus may be
worth proposing. A priority that waits on an operator decision, or that a dispatcher seat handed to him by raising it
again under its own name, is no trigger.
"""

import json
from dataclasses import replace

from scripts.inbox.store import InboxStore
from scripts.swarm import bottleneck, dev_red, lane_split, launch_check, lifetime, priority_sweep, seat_spawn
from scripts.swarm.store import DISPATCH, FULL

LANE = DISPATCH
SEAT = "dispatcher"
SENDER = "swarm"
STALE_MS = 15 * 60 * 1000
SENT = "dispatch-sent"
LEFT = "dispatch-left"
CLOSED = "its triggers closed"
DEPARTED = "it left the ledger"
ENDED_STATES = ("stopping", "stopped")
PAUSED = "paused"
REFUSED = "dispatcher triggers are still open; settle them, or the tick ends your seat once they close:\n{lines}"
WAKE = "New dispatcher triggers in swarm {slug}:\n{lines}\nSettle each one, then tell the master what you did."
LINES = {
    "priority": "- The priority on {item} is unresolved after {minutes} minutes: {text}",
    "bottleneck": "- The bottleneck report named the same share {ticks} ticks running and no lane rule covers it: {text}",
    "freeze": "- Dev Tests run {run} is red and holds these blocked tasks: {text}. Propose a freeze or a focus to the "
    "master if one would help.",
}


def triggers(doc: dict, now_ms: int) -> list[dict]:
    return [
        {"id": row["id"], "item": row["item"], "text": row["text"], "minutes": (now_ms - row["at"]) // 60_000}
        for row in doc.get("priorities", [])
        if now_ms - row["at"] >= STALE_MS and not priority_sweep.operator_only(doc, row)
    ]


def uncovered(store, slug: str, now_ms: int) -> list[dict]:
    held = json.loads(store.redis.get(store.key(slug, lane_split.KEY)) or "{}")
    named = held.get("named")
    if not named or named in lane_split.MOVES or held["ticks"] < lane_split.TICKS or now_ms - held["at"] > STALE_MS:
        return []
    text = bottleneck.line(bottleneck.read(store, slug), now_ms)
    return [{"id": f"bottleneck:{named}", "kind": "bottleneck", "ticks": held["ticks"], "text": text}]


def red_dev(store, slug: str, doc: dict) -> list[dict]:
    held = store.redis.hgetall(dev_red.key(slug))
    blocked = sorted(t["id"] for t in doc.get("tasks", []) if t["id"] in held and t.get("state") == "blocked")
    if not blocked:
        return []
    run_id = max(int(held[task_id]) for task_id in blocked)
    return [{"id": f"dev-red:{run_id}", "kind": "freeze", "run": run_id, "text": ", ".join(blocked)}]


def line(trigger: dict) -> str:
    return LINES[trigger.get("kind", "priority")].format(**trigger)


def open_triggers(slug: str, config, store, doc: dict, now_ms: int, sleeping: bool) -> list[dict]:
    active = config.autonomy == FULL and config.state not in ENDED_STATES and not sleeping
    return triggers(doc, now_ms) + uncovered(store, slug, now_ms) + red_dev(store, slug, doc) if active else []


def refusal(slug: str, config, store, doc: dict, now_ms: int) -> str:
    sleeping = lifetime.sleeping(slug, store, {task["id"]: task for task in doc["tasks"]})
    found = open_triggers(slug, config, store, doc, now_ms, sleeping)
    return REFUSED.format(lines="\n".join(line(trigger) for trigger in found)) if found else ""


def run(slug: str, config, store, runtime, doc: dict, now_ms: int, sleeping: bool = False) -> list[str]:
    seats = [a for a in store.agents(slug) if a.lane == LANE and a.state != "finished"]
    found = open_triggers(slug, config, store, doc, now_ms, sleeping)
    if not found:
        store.redis.delete(store.key(slug, SENT), store.key(slug, LEFT))
        return [_end(slug, store, seat) for seat in seats]
    gone = [seat for seat in seats if left(seat, doc)]
    if gone:
        if seen := set(store.redis.hkeys(store.key(slug, SENT))) & {trigger["id"] for trigger in found}:
            store.redis.sadd(store.key(slug, LEFT), *seen)
        return [_end(slug, store, seat, DEPARTED) for seat in gone]
    if seats:
        return _wake(slug, store, seats[0], found)
    held = store.redis.smembers(store.key(slug, LEFT))
    if closed := held - {trigger["id"] for trigger in found}:
        store.redis.srem(store.key(slug, LEFT), *closed)
    if config.state == PAUSED or all(trigger["id"] in held for trigger in found):
        return []
    if refused := seat_spawn.no_slot(config, runtime, SEAT) or seat_spawn.host_hold(slug, store, now_ms, SEAT):
        return [refused]
    return _spawn(slug, config, store, runtime, found, now_ms)


def left(seat, doc: dict) -> bool:
    members = doc.get("_meta", {}).get("members", {})
    return seat.name not in members and launch_check.joined_at(seat, doc) is not None


def _spawn(slug, config, store, runtime, found, now_ms):
    from scripts.swarm import tick

    def prepare(record):
        return tick.primed(store, slug, record.seat, {"id": SEAT, "title": "Dispatch", "triggers": found})

    try:
        record, placed = seat_spawn.place(slug, config, store, runtime, LANE, now_ms, prepare)
    except seat_spawn.SeatFailed as failed:
        store.drop_agent(slug, failed.record.name)
        return [f"dispatcher spawn failed: {failed.error}"]
    store.put_agent(slug, tick.placed_record(record, placed))
    _sent(store, slug, found)
    return [f"spawned dispatcher {record.name} for {_count(found)}"]


def _wake(slug, store, seat, found):
    sent = set(store.redis.hkeys(store.key(slug, SENT)))
    new = [trigger for trigger in found if trigger["id"] not in sent]
    if not new:
        return []
    lines = "\n".join(line(trigger) for trigger in new)
    InboxStore(store.redis).send(SENDER, seat.seat, WAKE.format(slug=slug, lines=lines))
    _sent(store, slug, new)
    return [f"woke {seat.name} with {_count(new, 'new ')}"]


def _end(slug, store, seat, why=CLOSED):
    store.put_agent(slug, replace(seat, state="finished"))
    return f"ended dispatcher {seat.name}: {why}"


def _sent(store, slug, found):
    store.redis.hset(store.key(slug, SENT), mapping={t["id"]: json.dumps(t) for t in found})


def _count(found, kind=""):
    return f"{len(found)} {kind}trigger" + ("" if len(found) == 1 else "s")
