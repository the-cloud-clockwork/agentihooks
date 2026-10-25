"""The dispatcher's own LLM seat, `dispatcher@<slug>`, woken only on triggers the deterministic passes could not settle.

At full autonomy a trigger wakes the live seat through its inbox or, unless the swarm is paused, spawns it through the
seat spawn helper; below full, once every trigger closes, or once the seat left the ledger, the seat is marked finished
and the reap pass retires it. Triggers that a departed seat had received spawn no new seat; a new trigger spawns one
primed with every open trigger. A trigger is a priority the sweep left unresolved for fifteen minutes, a bottleneck no
lane rule covers named by three reports running, or a red dev holding blocked tasks, where a freeze or focus may be
worth proposing. A priority that waits on an operator decision, or that a dispatcher seat handed to him by raising it
again under its own name, is no trigger.
"""

import json
from dataclasses import replace

from scripts.inbox.store import InboxError, InboxStore
from scripts.swarm import (
    bottleneck,
    capacity,
    dev_red,
    launch_check,
    lifetime,
    priority_sweep,
    seat_spawn,
    stage_cover,
    templates,
)
from scripts.swarm.store import DISPATCH, FULL

LANE = DISPATCH
SEAT = "dispatcher"
SENDER = "swarm"
STALE_MS = 15 * 60 * 1000
STREAK = "bottleneck-streak"
TICKS = 3
SENT = "dispatch-sent"
LEFT = "dispatch-left"
CLOSED = "its triggers closed"
DEPARTED = "it left the ledger"
ENDED_STATES = ("stopping", "stopped")
WAITING = ("pending", "delivered")
PAUSED = "paused"
BOTTLENECK = "bottleneck:"
REFUSED = "dispatcher triggers are still open; settle them, or the tick ends your seat once they close:\n{lines}"
WAKE = "New dispatcher triggers in swarm {slug}:\n{lines}\nSettle each one, then tell the master what you did."
LINES = {
    "priority": "- The priority on {item} is unresolved after {minutes} minutes: {text}",
    "inbox": "- Accepted inbox work {item}: read it and reply, close with an outcome, or explicitly transfer ownership.",
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


def _routing_applied(store, slug: str, write: bool) -> bool:
    key = store.key(slug, "dispatcher-quota-lanes")
    decision = capacity.read(store, slug)
    effective = decision.get("effective", {})
    affected = json.loads(store.redis.get(key) or "{}")
    if not affected:
        affected = {
            lane: effective[lane]
            for lane, cap in decision.get("configured", {}).items()
            if effective.get(lane, cap) < cap
        }
        if affected and write:
            store.redis.set(key, json.dumps(affected))
        return False
    lanes = store.config(slug).lanes
    return all(
        lanes.get(lane, {}).get("agent", templates.AUTO) == templates.AUTO and effective.get(lane, 0) > cap
        for lane, cap in affected.items()
    )


def uncovered(store, slug: str, now_ms: int, write: bool = True) -> list[dict]:
    key = store.key(slug, STREAK)
    previous = json.loads(store.redis.get(key) or "{}")
    found = bottleneck.read(store, slug)
    held = streak(previous, found)
    if write and held is not previous:
        store.redis.set(key, json.dumps(held))
    named = held.get("named")
    if not named or held["ticks"] < TICKS or now_ms - held["at"] > STALE_MS:
        _forget_quota_lanes(store, slug, write)
        return []
    if named != "quota":
        _forget_quota_lanes(store, slug, write)
    elif _routing_applied(store, slug, write):
        return []
    text = bottleneck.line(found, now_ms)
    return [{"id": f"{BOTTLENECK}{named}", "kind": "bottleneck", "ticks": held["ticks"], "text": text}]


def streak(previous: dict, found: dict) -> dict:
    if not found or found.get("at") == previous.get("at"):
        return previous
    named = found["bottleneck"]
    ticks = previous["ticks"] + 1 if named == previous.get("named") else 1
    return {"named": named, "ticks": ticks, "at": found["at"]}


def _forget_quota_lanes(store, slug: str, write: bool) -> None:
    if write:
        store.redis.delete(store.key(slug, "dispatcher-quota-lanes"))


def red_dev(store, slug: str, doc: dict) -> list[dict]:
    held = store.redis.hgetall(dev_red.key(slug))
    blocked = sorted(t["id"] for t in doc.get("tasks", []) if t["id"] in held and t.get("state") == "blocked")
    if not blocked:
        return []
    run_id = max(int(held[task_id]) for task_id in blocked)
    return [{"id": f"dev-red:{run_id}", "kind": "freeze", "run": run_id, "text": ", ".join(blocked)}]


def line(trigger: dict) -> str:
    return LINES[trigger.get("kind", "priority")].format(**trigger)


def inbox_work(store, slug: str) -> list[dict]:
    inbox = InboxStore(store.redis)
    items = {item.id: item for address in _addresses(store, slug) for item in inbox.open_mail(address)}
    return [
        {"id": f"inbox:{item.id}", "kind": "inbox", "item": item.id}
        for item in items.values()
        if not item.fyi and not (item.sender == SENDER and item.text.startswith("New dispatcher triggers in swarm "))
    ]


def _addresses(store, slug: str) -> list[str]:
    return [f"{SEAT}@{slug}", *(a.name for a in store.agents(slug) if a.lane == LANE and a.state != "finished")]


def open_triggers(slug: str, config, store, doc: dict, now_ms: int, sleeping: bool, write: bool = True) -> list[dict]:
    active = config.autonomy == FULL and config.state not in ENDED_STATES and not sleeping
    if not active:
        return []
    held = [
        trigger
        for trigger in uncovered(store, slug, now_ms, write)
        if not stage_cover.covered(store, slug, doc, trigger["id"].removeprefix(BOTTLENECK), share=True, write=write)
    ]
    return triggers(doc, now_ms) + held + red_dev(store, slug, doc) + inbox_work(store, slug)


def refusal(slug: str, config, store, doc: dict, now_ms: int) -> str:
    sleeping = lifetime.sleeping(slug, store, {task["id"]: task for task in doc["tasks"]}, write=False)
    found = open_triggers(slug, config, store, doc, now_ms, sleeping, write=False)
    return REFUSED.format(lines="\n".join(line(trigger) for trigger in found)) if found else ""


def run(slug: str, config, store, runtime, doc: dict, now_ms: int, sleeping: bool = False) -> list[str]:
    seats = [a for a in store.agents(slug) if a.lane == LANE and a.state != "finished"]
    found = open_triggers(slug, config, store, doc, now_ms, sleeping)
    if not found:
        store.redis.delete(store.key(slug, SENT), store.key(slug, LEFT))
        return [_end(slug, store, seat) for seat in seats]
    gone = [seat for seat in seats if left(seat, doc)]
    if gone:
        if seen := set(store.redis.hkeys(store.key(slug, SENT))) & {
            trigger["id"] for trigger in found if trigger.get("kind") != "inbox"
        }:
            store.redis.sadd(store.key(slug, LEFT), *seen)
        return [_end(slug, store, seat, DEPARTED) for seat in gone]
    if seats:
        return _wake(slug, store, seats[0], found)
    held = store.redis.smembers(store.key(slug, LEFT))
    if closed := held - {trigger["id"] for trigger in found}:
        store.redis.srem(store.key(slug, LEFT), *closed)
    if config.state == PAUSED or all(trigger["id"] in held for trigger in found):
        return []
    if refused := (
        seat_spawn.placed_elsewhere(runtime, LANE, {"id": SEAT})
        or seat_spawn.no_slot(config, runtime, SEAT)
        or seat_spawn.host_hold(slug, store, now_ms, SEAT)
    ):
        return [refused]
    return _spawn(slug, config, store, runtime, found, now_ms)


def left(seat, doc: dict) -> bool:
    members = doc.get("_meta", {}).get("members", {})
    return seat.name not in members and launch_check.joined_at(seat, doc) is not None


class _NoTriggers(Exception):
    pass


def _launch_triggers(store, slug, found):
    inbox = InboxStore(store.redis)
    owners = set(_addresses(store, slug))
    live = []
    for trigger in found:
        if trigger.get("kind") == "inbox":
            try:
                item = inbox.get(trigger["item"])
            except InboxError:
                continue
            if item.state not in WAITING or inbox.names.resolve(item.address) not in owners:
                continue
        live.append(trigger)
    return live


def _spawn(slug, config, store, runtime, found, now_ms):
    found = _launch_triggers(store, slug, found)
    if not found:
        return []
    from scripts.swarm import tick

    def prepare(record):
        nonlocal found
        task = tick.primed(store, slug, record.seat, {"id": SEAT, "title": "Dispatch"})
        found = _launch_triggers(store, slug, found)
        if not found:
            raise _NoTriggers
        task["triggers"] = found
        return task

    try:
        record, placed = seat_spawn.place(slug, config, store, runtime, LANE, now_ms, prepare)
    except seat_spawn.SeatFailed as failed:
        store.drop_agent(slug, failed.record.name)
        if isinstance(failed.error, _NoTriggers):
            return []
        return [f"dispatcher spawn failed: {failed.error}"]
    store.put_agent(slug, tick.placed_record(record, placed))
    _sent(store, slug, found)
    return [f"spawned dispatcher {record.name} for {_count(found)}"]


def _wake(slug, store, seat, found):
    sent = set(store.redis.hkeys(store.key(slug, SENT)))
    new = [trigger for trigger in found if trigger["id"] not in sent and trigger.get("kind") != "inbox"]
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
