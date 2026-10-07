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


def settle_unresolved(
    inbox: InboxStore, slug: str, names: set[str], items: list, now_ms: int, window: int
) -> list[str]:
    import re

    live, actions = None, []
    for item in items:
        address = inbox.names.resolve(item.address)
        belongs = of_swarm(address, slug, inbox.names) or of_swarm(item.sender, slug, inbox.names)
        session = re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", address)
        if not (belongs or session) or now_ms - item.created_at < window:
            continue
        if live is None:
            sessions = get_active_sessions(cleanup=True)
            live = names | {n for sid, row in sessions.items() for n in (sid, row.get("name", "")) if n}
        if not resolves(inbox, address, live):
            if inbox.withdraw(item.id, "swarm", f"cancelled: recipient no longer resolves: {address}", item.address):
                actions.append(f"closed message {item.id}: recipient no longer resolves")
    return actions
