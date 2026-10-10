"""The dispatcher's own LLM seat, `dispatcher@<slug>`, woken only on triggers the deterministic passes could not settle.

At full autonomy a trigger wakes the live seat through its inbox or spawns it through the seat spawn helper; below
full, or once every trigger closes, the seat is marked finished and the reap pass retires it. A trigger is a priority
the sweep left unresolved for fifteen minutes, a bottleneck no lane rule covers held for the lane split's ticks, or a
red dev holding blocked tasks, where a freeze or focus may be worth proposing.
"""

import json
from dataclasses import replace

from scripts.inbox.store import InboxStore
from scripts.swarm import bottleneck, dev_red, lane_split, seat_spawn
from scripts.swarm.store import DISPATCH, FULL

LANE = DISPATCH
SEAT = "dispatcher"
SENDER = "swarm"
STALE_MS = 15 * 60 * 1000
SENT = "dispatch-sent"
ENDED_STATES = ("stopping", "stopped")
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
        if now_ms - row["at"] >= STALE_MS
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


def run(slug: str, config, store, runtime, doc: dict, now_ms: int, sleeping: bool = False) -> list[str]:
    seats = [a for a in store.agents(slug) if a.lane == LANE and a.state != "finished"]
    active = config.autonomy == FULL and config.state not in ENDED_STATES and not sleeping
    found = triggers(doc, now_ms) + uncovered(store, slug, now_ms) + red_dev(store, slug, doc) if active else []
    if not found:
        store.redis.delete(store.key(slug, SENT))
        return [_end(slug, store, seat) for seat in seats]
    if seats:
        return _wake(slug, store, seats[0], found)
    if refused := seat_spawn.no_slot(config, runtime, SEAT) or seat_spawn.host_hold(slug, store, now_ms, SEAT):
        return [refused]
    return _spawn(slug, config, store, runtime, found, now_ms)


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


def _end(slug, store, seat):
    store.put_agent(slug, replace(seat, state="finished"))
    return f"ended dispatcher {seat.name}: its triggers closed"


def _sent(store, slug, found):
    store.redis.hset(store.key(slug, SENT), mapping={t["id"]: json.dumps(t) for t in found})


def _count(found, kind=""):
    return f"{len(found)} {kind}trigger" + ("" if len(found) == 1 else "s")
