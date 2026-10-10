"""The dispatcher's own LLM seat, `dispatcher@<slug>`, woken only on triggers the deterministic passes could not settle.

At full autonomy a trigger wakes the live seat through its inbox or spawns it through the seat spawn helper; below
full, or once every trigger closes, the seat is marked finished and the reap pass retires it. A trigger is a priority
the sweep left unresolved for fifteen minutes.
"""

import json
from dataclasses import replace

from scripts.inbox.store import InboxStore
from scripts.swarm import seat_spawn
from scripts.swarm.store import DISPATCH, FULL

LANE = DISPATCH
SEAT = "dispatcher"
SENDER = "swarm"
STALE_MS = 15 * 60 * 1000
SENT = "dispatch-sent"
ENDED_STATES = ("stopping", "stopped")
WAKE = "New dispatcher triggers in swarm {slug}:\n{lines}\nSettle each one, then tell the master what you did."


def triggers(doc, now_ms):
    return [
        {"id": row["id"], "item": row["item"], "text": row["text"], "minutes": (now_ms - row["at"]) // 60_000}
        for row in doc.get("priorities", [])
        if now_ms - row["at"] >= STALE_MS
    ]


def line(trigger):
    return f"- The priority on {trigger['item']} is unresolved after {trigger['minutes']} minutes: {trigger['text']}"


def run(slug, config, store, runtime, doc, now_ms, sleeping=False):
    seats = [a for a in store.agents(slug) if a.lane == LANE and a.state != "finished"]
    active = config.autonomy == FULL and config.state not in ENDED_STATES and not sleeping
    found = triggers(doc, now_ms) if active else []
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
