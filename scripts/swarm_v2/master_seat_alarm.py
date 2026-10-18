"""A master seat two and up whose launch keeps failing: after as many failed launches as exhaust the lead's automatic
retry, the operator is told once which seat is down, the phases it owns and the exact launch error. A master binding in
the seat ends the alarm and tells the operator the seat is live again."""

import json

from scripts.inbox.seats import seat_address
from scripts.swarm_v2 import masters

KEY = "master-seat-failures"
LAUNCHES = 2
DOWN = (
    "Master seat {address} failed {count} launches in a row, so the phases it owns go unattended: {held}. Last launch "
    "error: {error}. Operator action is required."
)
BACK = "Master seat {address} is live again after its failed launches and answers the phases it owns: {held}."
RETIRED = "Master seat {address} left the master count, so its launch alarm needs no operator action any more."


def failed(store, slug: str, seat_name: str, error: str) -> None:
    raw = store.redis.hget(store.key(slug, KEY), seat_name)
    state = json.loads(raw) if raw else {}
    _save(store, slug, seat_name, {**state, "count": state.get("count", 0) + 1, "error": error})


def run(slug: str, store, ledger, runtime, doc: dict) -> list[str]:
    key = store.key(slug, KEY)
    states = {seat_name: json.loads(raw) for seat_name, raw in store.redis.hgetall(key).items()}
    if not states:
        return []
    seats = masters.MasterSeats(store.redis)
    counted = set(masters.seats(slug, seats.count(slug)))
    addresses = {seat_address(slug, seat_name) for seat_name in states}
    held_seats = [a for a in store.agents(slug) if a.seat in addresses]
    bound = {a.seat for a in held_seats if a.state != "finished" and runtime.reported(a)}
    owners, actions = seats.owners(slug, doc), []
    for seat_name, state in sorted(states.items()):
        address = seat_address(slug, seat_name)
        held = _held(owners, address)
        if address not in counted:
            if state.get("alerted"):
                ledger.notify(slug, RETIRED.format(address=address))
                actions.append(f"closed the launch alarm of retired master seat {address}")
            store.redis.hdel(key, seat_name)
        elif address in bound:
            if state.get("alerted"):
                ledger.notify(slug, BACK.format(address=address, held=held))
                actions.append(f"cleared the launch alarm of master seat {address}")
            store.redis.hdel(key, seat_name)
        elif state["count"] >= LAUNCHES and not state.get("alerted"):
            ledger.notify(slug, DOWN.format(address=address, count=state["count"], held=held, error=state["error"]))
            _save(store, slug, seat_name, {**state, "alerted": True})
            actions.append(f"raised the launch alarm of master seat {address}")
    return actions


def _held(owners: dict, address: str) -> str:
    owned = sorted(item for item, owner in owners.items() if owner == address)
    return ", ".join(owned) or "none"


def _save(store, slug, seat_name, state):
    store.redis.hset(store.key(slug, KEY), seat_name, json.dumps(state))
