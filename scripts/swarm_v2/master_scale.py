"""The master count follows the swarm size: one master seat per configured number of working agents, or per hive.

A larger swarm raises the count at once and the tick launches a master in each empty seat. A smaller one, once it has
stayed smaller for the hold window, asks each extra seat's master to hand off; the count falls only past seats whose
master has left, so no seat is cut mid answer. Each count change rebalances phase ownership by code and posts the new
owners on the masters channel. Without a rule the count stays the manual one and the tick still fills its seats.
"""

import json
from dataclasses import replace

from scripts.inbox.seats import seat_address
from scripts.inbox.store import InboxStore
from scripts.swarm import seat_spawn
from scripts.swarm.store import MASTER
from scripts.swarm_v2 import masters
from scripts.swarm_v2.masters_channel import MastersChannel

OFF = "off"
PER_HIVE = "hive"
RULE = "per"
BELOW = "below"
RETIRING = "retiring"
SENDER = "swarm"
HOLD_MS = 10 * 60_000
ENDED = ("stopping", "stopped")
ASK = (
    "Swarm {slug} shrank to {want} master seats, so seat {address} retires. Write your Handoff v2 for the lead master "
    "{lead}, run agentihooks swarm {slug} handoff <doc> and stop; the tick then moves your phases and pending items to "
    "the remaining masters."
)
HANDED = "Master seat {address} of swarm {slug} retired as the swarm shrank. Its handoff document follows.\n\n"


def rule_of(text: str) -> str:
    if text in (OFF, PER_HIVE):
        return "" if text == OFF else text
    if not text.isdecimal() or int(text) < 1:
        raise masters.MasterError(f"master-per takes a whole number of agents per master, hive or off, not {text!r}")
    return text


def set_rule(redis, slug: str, rule: str) -> None:
    key = masters.MasterSeats(redis).key(slug)
    if rule:
        redis.hset(key, RULE, rule)
    else:
        redis.hdel(key, RULE)


def rule(redis, slug: str) -> str:
    return redis.hget(masters.MasterSeats(redis).key(slug), RULE) or ""


def working(agents: list) -> list:
    return [a for a in agents if a.lane != MASTER and a.state != "finished"]


def wanted(rule: str, agents: list) -> int:
    found = working(agents)
    if rule == PER_HIVE:
        return max(1, len({a.hive for a in found}))
    return max(1, -(-len(found) // int(rule)))


def run(slug: str, config, store, runtime, doc: dict, now_ms: int) -> list[str]:
    if config.state in ENDED:
        return []
    agents = [a for a in store.agents(slug) if a.state != "finished"]
    count = masters.MasterSeats(store.redis).count(slug)
    found = rule(store.redis, slug)
    want = wanted(found, agents) if found else count
    actions = _resize(slug, store, doc, agents, count, want, now_ms)
    limit = min(masters.MasterSeats(store.redis).count(slug), want)
    return actions + _fill(slug, config, store, runtime, doc, limit, now_ms)


def _resize(slug, store, doc, agents, count, want, now_ms):
    key = masters.MasterSeats(store.redis).key(slug)
    if want >= count:
        store.redis.hdel(key, BELOW, RETIRING)
        return _recount(slug, store, doc, agents, count, want, now_ms) if want > count else []
    below = int(store.redis.hget(key, BELOW) or 0)
    if not below:
        store.redis.hset(key, BELOW, now_ms)
    if now_ms - (below or now_ms) < HOLD_MS:
        return []
    occupied = {a.seat: a for a in agents if a.lane == MASTER}
    retiring = json.loads(store.redis.hget(key, RETIRING) or "{}")
    actions = _ask(slug, store, occupied, retiring, range(count, want, -1), want, now_ms)
    keep = max([want] + [i for i in range(want + 1, count + 1) if _address(slug, i) in occupied])
    for index in range(count, keep, -1):
        address = _address(slug, index)
        actions.append(_retire(slug, store, address, retiring.pop(address, [now_ms, ""]), now_ms))
    store.redis.hset(key, RETIRING, json.dumps(retiring))
    return actions + (_recount(slug, store, doc, agents, count, keep, now_ms) if keep < count else [])


def _address(slug: str, index: int) -> str:
    return seat_address(slug, masters.seat(index))


def _ask(slug, store, occupied, retiring, indexes, want, now_ms):
    actions, lead = [], _address(slug, 1)
    for index in indexes:
        address = _address(slug, index)
        agent = occupied.get(address)
        if agent and address not in retiring:
            text = ASK.format(slug=slug, want=want, address=address, lead=lead)
            retiring[address] = [now_ms, InboxStore(store.redis).send(SENDER, address, text).id]
            actions.append(f"asked {agent.name} to hand off retiring seat {address}")
    return actions


def _retire(slug, store, address, asked, now_ms):
    inbox, lead, seat_name = InboxStore(store.redis), _address(slug, 1), address.removesuffix(f"@{slug}")
    asked_at, ask_id = asked
    text = store.handoff(slug, seat_name)
    if text:
        inbox.send(SENDER, lead, HANDED.format(address=address, slug=slug) + text)
        store.clear_handoff(slug, seat_name)
    for item in inbox.pending_items(address):
        if item.id == ask_id:
            inbox.withdraw(item.id, SENDER, f"master seat {address} retired")
        else:
            inbox.redirect(item.id, SENDER, lead, f"master seat {address} retired")
    return f"retired master seat {address} after {(now_ms - asked_at) // 1000} seconds"


def _recount(slug, store, doc, agents, before, after, now_ms):
    seats = masters.MasterSeats(store.redis)
    seats.set_count(slug, after)
    owners, size = seats.owners(slug, doc), len(working(agents))
    MastersChannel(store.redis).announce(slug, announcement(slug, after, size, owners), now_ms / 1000)
    return [f"master seats {before} to {after} for {size} working agents"]


def announcement(slug: str, count: int, size: int, owners: dict[str, str]) -> str:
    held = [
        f"{address} owns {', '.join(p for p, owner in owners.items() if owner == address) or 'no phase'}"
        for address in masters.seats(slug, count)
    ]
    return f"Master seats are now {count} for {size} working agents. Phase owners: {'; '.join(held)}."


def _fill(slug, config, store, runtime, doc, limit, now_ms):
    live = runtime.live_names()
    taken = {a.seat for a in store.agents(slug) if a.lane == MASTER and (a.state != "finished" or a.name in live)}
    actions = []
    for index in range(2, limit + 1):
        if _address(slug, index) in taken:
            continue
        if refused := seat_spawn.no_slot(config, runtime, MASTER) or seat_spawn.host_hold(slug, store, now_ms, MASTER):
            return actions + [refused]
        actions.append(_launch(slug, config, store, runtime, doc, index, now_ms))
    return actions


def _launch(slug, config, store, runtime, doc, index, now_ms):
    from scripts.swarm import tick

    seats, seat_name = masters.MasterSeats(store.redis), masters.seat(index)
    owners, count = seats.owners(slug, doc), seats.count(slug)

    def prepare(record):
        phases = [p for p, owner in owners.items() if owner == record.seat]
        task = {"id": seat_name, "handoff": store.handoff(slug, seat_name), "phases": phases, "masters": count}
        return tick.primed(store, slug, record.seat, task)

    try:
        record, placed = seat_spawn.place_seat(
            slug, config, store, runtime, seat_spawn.Seat(MASTER, seat_name), now_ms, prepare
        )
    except seat_spawn.SeatFailed as failed:
        store.drop_agent(slug, failed.record.name)
        return f"master spawn failed in seat {failed.record.seat}: {failed.error}"
    store.put_agent(slug, replace(tick.placed_record(record, placed), state="working"))
    store.clear_handoff(slug, seat_name)
    return f"spawned master {record.name} in seat {record.seat}"
