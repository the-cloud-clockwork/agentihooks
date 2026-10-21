from hooks.context.broadcast import get_active_sessions
from scripts.inbox.seats import is_seat, of_swarm
from scripts.inbox.store import InboxError, InboxStore


def check_address(inbox: InboxStore, sender: str, address: str) -> None:
    resolved = inbox.names.resolve(address)
    if resolved == "operator" or inbox.names.entry(resolved):
        return
    if is_seat(resolved) and inbox.seats.occupant(resolved).generation:
        return
    sessions = get_active_sessions(cleanup=True)
    live = {name for sid, row in sessions.items() for name in (sid, row.get("name", "")) if name}
    if resolved in live:
        return
    seat = sender if is_seat(sender) else inbox.seats.seat_of(sender)
    slug = seat.split("@", 1)[1] if seat else inbox.names.slug_of(sender)
    valid = {"operator"}
    if slug:
        valid.update(row["name"] for row in inbox.names.names(slug))
        valid.update(name for name in live if of_swarm(name, slug, inbox.names))
        prefix = inbox.seats.key("")
        valid.update(
            key[len(prefix) :]
            for key in inbox.redis.scan_iter(match=f"{prefix}*@{slug}")
            if inbox.redis.type(key) == "hash"
        )
    else:
        valid.update(live)
    raise InboxError(f"unknown inbox address {address}; valid addresses: {', '.join(sorted(valid))}")


def dispatcher_status(inbox: InboxStore, address: str) -> dict:
    from scripts.swarm.store import DISPATCH, FULL, RedisStore

    seat = address if is_seat(address) else inbox.seats.known_seat(address)
    if not seat.startswith("dispatcher@"):
        return {}
    slug = seat.removeprefix("dispatcher@")
    store = RedisStore(inbox.redis)
    owner = next((a.name for a in store.agents(slug) if a.lane == DISPATCH and a.state != "finished"), "")
    activation = "recorded consumer" if owner else "pending lifecycle checks"
    if raw := inbox.redis.hgetall(store.key(slug, "config")):
        if raw["state"] in ("stopping", "stopped") or (raw["state"] == "paused" and not owner):
            activation = raw["state"]
        elif raw.get("autonomy") != FULL:
            activation = "autonomy"
    return {"dispatch": {"owner": seat, "consumer": owner, "activation": activation}}


def resolves(inbox: InboxStore, address: str, live: set[str]) -> bool:
    address = inbox.names.resolve(address)
    if address == "operator" or address in live:
        return True
    if is_seat(address):
        return bool(inbox.seats.occupant(address).generation)
    entry = inbox.names.entry(address)
    if entry and not entry.get("retired_at"):
        return True
    seat = inbox.seats.known_seat(address)
    outcome = inbox.seats.exit_of(address)
    return bool(seat and (not outcome or outcome["seat"]))


def settle_unresolved(inbox: InboxStore, names: set[str], items: list, now_ms: int, window: int) -> list[str]:
    live, actions, known = None, [], set()
    resolved = inbox.names.resolve_many(list(dict.fromkeys(item.address for item in items)))
    for item in items:
        address = resolved[item.address]
        if now_ms - item.created_at < window:
            continue
        if live is None:
            sessions = get_active_sessions(cleanup=True)
            live = names | {n for sid, row in sessions.items() for n in (sid, row.get("name", "")) if n}
        if address in known:
            continue
        if resolves(inbox, address, live):
            known.add(address)
            continue
        if inbox.withdraw(item.id, "swarm", f"cancelled: recipient no longer resolves: {address}", item.address):
            actions.append(f"closed message {item.id}: recipient no longer resolves")
    return actions
