from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from scripts.inbox.seats import is_seat
from scripts.swarm import fencing
from scripts.swarm.execution import TERMINAL_STATES

if TYPE_CHECKING:
    from scripts.inbox.store import InboxStore, Item
    from scripts.swarm.store import RedisStore

ENDED = "its recipient execution ended; not passed to the next occupant"


@dataclass(frozen=True)
class Recipient:
    name: str
    execution: str


def recipient(value: str | Recipient) -> tuple[str, str]:
    return (value.name, value.execution) if isinstance(value, Recipient) else (value, "")


def recipient_for(store: "RedisStore", name: str) -> Recipient:
    name = store.names.resolve(name)
    slug = store.names.slug_of(name)
    row = fencing.records(store, slug).record(slug, fencing.BY_NAME, name)
    return Recipient(name, str(row.value) if row is not None else "")


def expired(inbox: "InboxStore", item: "Item", pipe: Any = None) -> bool:
    from scripts.swarm.store import RedisStore

    if not item.execution:
        return False
    slug = item.address.partition("@")[2] if is_seat(item.address) else inbox.names.slug_of(item.address)
    store = RedisStore(inbox.redis)
    if pipe is not None:
        fencing.watch(store, pipe)
    records = fencing.records(store, slug)
    row = records.record(slug, "executions", item.execution)
    if row is None or row.value["state"] in TERMINAL_STATES:
        return True
    seat = row.value["seat"]
    head = records.record(slug, fencing.BY_SEAT, seat)
    if head is None or head.value != item.execution:
        return True
    occupant = inbox.seats.watch(pipe, seat) if pipe is not None else inbox.seats.occupant(seat)
    return occupant is None or occupant.occupant != row.value["name"] or inbox.seats.left(row.value["name"], seat)


def close_expired(inbox: "InboxStore", item: "Item") -> bool:
    if not expired(inbox, item):
        return False
    inbox.settle(item.id, item.sender, "done", ENDED)
    return True


def visible(inbox: "InboxStore", items: list["Item"]) -> list["Item"]:
    return [item for item in items if not close_expired(inbox, item)]


def transition(inbox: "InboxStore", item: "Item", state: str, reason: str, pipe: Any) -> tuple[str, str, bool]:
    if state in ("delivered", "read", "confirmed") and expired(inbox, item, pipe):
        return "done", f"done: {ENDED}", True
    return state, reason, False


def result(item: "Item", discard: bool) -> "Item | None":
    return None if discard else item


def settle_exit(inbox: "InboxStore", name: str) -> None:
    from scripts.swarm.store import RedisStore

    held = inbox.seats.known_seat(name)
    slug = inbox.names.slug_of(name)
    records = fencing.records(RedisStore(inbox.redis), slug)
    for address in (name, held) if held else (name,):
        for item in inbox.open_items(address):
            if not item.execution:
                continue
            row = records.record(slug, "executions", item.execution) or records.record(slug, "archive", item.execution)
            if row is not None and row.value["name"] == name:
                inbox.settle(item.id, item.sender, "done", ENDED)
