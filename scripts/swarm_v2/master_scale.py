"""The master count follows the swarm size: one master seat per configured number of working agents, or per hive.

A larger swarm raises the count at once and master_fill launches a master in each empty seat. A smaller one, once it has
stayed smaller for the hold window, asks each extra seat's master to hand off; the count falls only past seats whose
master has left, so no seat is cut mid answer. Each count change rebalances phase ownership by code and posts the new
owners on the masters channel. Without a rule the swarm's masters step sizes the count; with step 0 the count stays the
manual one and the tick still fills its seats.
"""

import json

from scripts.inbox.seats import seat_address
from scripts.inbox.store import CLOSED, InboxStore
from scripts.swarm import metrics_outbox
from scripts.swarm.store import MASTER
from scripts.swarm_v2 import master_fill, masters
from scripts.swarm_v2.masters_channel import SWARM, MastersChannel

OFF = "off"
PER_HIVE = "hive"
RULE = "per"
BELOW = "below"
BELOW_WANT = "below-want"
RETIRING = "retiring"
RETIRE_SECONDS = "retire-seconds"
TABLE = metrics_outbox.Table("master_scaling", (("metric", "String"), ("value", "Int64"), ("seat", "String")))
SENDER = SWARM
HOLD_MS = 10 * 60_000
ENDED = ("stopping", "stopped")
ASK = (
    "Swarm {slug} shrank to {want} master seats, so seat {address} retires. Write your Handoff v2 for the lead master "
    "{lead}, run agentihooks swarm {slug} handoff <doc> and stop; the tick then moves your phases and pending items to "
    "the lead master."
)
HANDED = "Master seat {address} of swarm {slug} retired as the swarm shrank. Its handoff document follows.\n\n"
EARLY = "Master seat {address} retired; its phases moved: {moved}."
EARLY_EMPTY = "Master seat {address} retired owning no phase."


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


def stepped(step: int, agents: list) -> int:
    return 1 + len(working(agents)) // step


def run(slug: str, config, store, runtime, doc: dict, now_ms: int) -> list[str]:
    seats, found = masters.MasterSeats(store.redis), rule(store.redis, slug)
    if not (found or config.masters_step) or config.state in ENDED:
        store.redis.hdel(seats.key(slug), masters.RETIRED)
        return master_fill.run(slug, config, store, runtime, doc, now_ms)
    agents = [a for a in store.agents(slug) if a.state != "finished"]
    want = wanted(found, agents) if found else stepped(config.masters_step, agents)
    actions = _resize(slug, store, doc, agents, seats.count(slug), want, now_ms)
    return actions + master_fill.run(slug, config, store, runtime, doc, now_ms, min(seats.count(slug), want))


def _resize(slug, store, doc, agents, count, want, now_ms):
    key = masters.MasterSeats(store.redis).key(slug)
    retiring = json.loads(store.redis.hget(key, RETIRING) or "{}")
    masters.MasterSeats(store.redis).keep_retired(slug, want, count)
    if want >= count:
        _withdraw_asks(store, retiring)
        store.redis.hdel(key, BELOW, RETIRING)
        return _recount(slug, store, doc, agents, count, want, now_ms) if want > count else []
    _withdraw_wanted_again(slug, store, key, retiring, want)
    if now_ms - _below_since(store.redis, key, want, now_ms) < HOLD_MS:
        return []
    occupied = {a.seat: a for a in agents if a.lane == MASTER}
    actions = _ask(slug, store, occupied, retiring, range(count, want, -1), want, now_ms)
    keep = max([want] + [i for i in range(want + 1, count + 1) if _address(slug, i) in occupied])
    for index in range(count, want, -1):
        address = _address(slug, index)
        if index > keep:
            actions.append(_retire(slug, store, address, retiring.pop(address, None), now_ms))
        elif address in retiring and address not in occupied:
            actions.append(_retire_early(slug, store, doc, address, retiring.pop(address), now_ms))
    store.redis.hset(key, RETIRING, json.dumps(retiring))
    return actions + (_recount(slug, store, doc, agents, count, keep, now_ms) if keep < count else [])


def _below_since(redis, key: str, want: int, now_ms: int) -> int:
    since, last = int(redis.hget(key, BELOW) or 0), int(redis.hget(key, BELOW_WANT) or want)
    if not since or want > last:
        since = now_ms
    redis.hset(key, mapping={BELOW: since, BELOW_WANT: want})
    return since


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
    """asked is [ask time, ask item id] when the seat's master was asked to hand off, else None."""
    inbox, lead, seat_name = InboxStore(store.redis), _address(slug, 1), address.removesuffix(f"@{slug}")
    ask_id = asked[1] if asked else ""
    masters.MasterSeats(store.redis).retire(slug, address)
    text = store.handoff(slug, seat_name)
    if text:
        inbox.send(SENDER, lead, HANDED.format(address=address, slug=slug) + text)
        store.clear_handoff(slug, seat_name)
    for item in inbox.inbox(address):
        if item.state in CLOSED:
            continue
        if item.id == ask_id:
            inbox.withdraw(item.id, SENDER, f"master seat {address} retired")
        else:
            inbox.redirect(item.id, SENDER, lead, f"master seat {address} retired")
    if not asked:
        return f"closed empty master seat {address}"
    seconds = (now_ms - asked[0]) // 1000
    store.redis.rpush(f"{masters.MasterSeats(store.redis).key(slug)}:{RETIRE_SECONDS}", seconds)
    _measure(slug, "master_retire_seconds", seconds, address, now_ms)
    return f"retired master seat {address} after {seconds} seconds"


def _retire_early(slug, store, doc, address, asked, now_ms):
    """A seat below a live higher seat stays counted; its phases move to the front door in the same step."""
    seats = masters.MasterSeats(store.redis)
    owned = [p for p, owner in seats.owners(slug, doc).items() if owner == address]
    action = _retire(slug, store, address, asked, now_ms)
    owners = seats.owners(slug, doc)
    moved = ", ".join(f"{p} to {owners.get(p, _address(slug, 1))}" for p in owned)
    text = EARLY.format(address=address, moved=moved) if owned else EARLY_EMPTY.format(address=address)
    MastersChannel(store.redis).announce(slug, text, now_ms / 1000)
    return action


def _withdraw_asks(store, retiring):
    inbox = InboxStore(store.redis)
    for address, (_, ask_id) in retiring.items():
        inbox.withdraw(ask_id, SENDER, f"the swarm grew again, so seat {address} stays", expected_address=address)


def _withdraw_wanted_again(slug, store, key, retiring, want):
    wanted_again = [address for address in retiring if address in masters.seats(slug, want)]
    if wanted_again:
        _withdraw_asks(store, {address: retiring.pop(address) for address in wanted_again})
        store.redis.hset(key, RETIRING, json.dumps(retiring))


def _measure(slug, metric, value, seat, now_ms):
    from scripts.swarm import metrics

    row = {
        "event_id": f"master-scaling:{slug}:{metric}:{seat}:{now_ms}:{value}",
        "ledger": slug,
        "ts_ms": now_ms,
        "plan": "",
        "phase": "",
        "slice": "",
        "task": "",
        "metric": metric,
        "value": value,
        "seat": seat,
    }
    metrics.record(TABLE, [row], now_ms)


def measurements(redis, slug: str) -> dict:
    key = masters.MasterSeats(redis).key(slug)
    return {
        "master_seat_count": masters.MasterSeats(redis).count(slug),
        "master_retire_seconds": [int(s) for s in redis.lrange(f"{key}:{RETIRE_SECONDS}", 0, -1)],
    }


def _recount(slug, store, doc, agents, before, after, now_ms):
    seats = masters.MasterSeats(store.redis)
    seats.set_count(slug, after)
    _measure(slug, "master_seat_count", after, "", now_ms)
    owners, size = seats.owners(slug, doc), len(working(agents))
    MastersChannel(store.redis).announce(slug, announcement(slug, after, size, owners), now_ms / 1000)
    return [f"master seats {before} to {after} for {size} working agents"]


def announcement(slug: str, count: int, size: int, owners: dict[str, str]) -> str:
    held = [
        f"{address} owns {', '.join(p for p, owner in owners.items() if owner == address) or 'no phase'}"
        for address in masters.seats(slug, count)
    ]
    return f"Master seats are now {count} for {size} working agents. Phase owners: {'; '.join(held)}."
