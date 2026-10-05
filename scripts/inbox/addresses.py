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
