from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from scripts.inbox.seats import Occupancy
from scripts.swarm import fencing
from scripts.swarm.execution import TERMINAL_STATES

if TYPE_CHECKING:
    from scripts.inbox.store import InboxStore, Item
    from scripts.swarm.store import RedisStore

ENDED = "its recipient execution ended; not passed to the next occupant"


class _Names(Protocol):
    def resolve(self, name: str, /) -> str: ...
    def slug_of(self, name: str, /) -> str: ...


class _Seats(Protocol):
    def occupant(self, address: str) -> Occupancy | None: ...
    def watch(self, pipe: Any, address: str) -> Occupancy | None: ...


@dataclass(frozen=True)
class Recipient:
    name: str
    execution: str


def _address(names: _Names, address: str) -> tuple[str, str]:
    name = names.resolve(address)
    return name, names.slug_of(name) or name.partition("@")[2]


def _occupant(seats: _Seats, pipe: Any, seat: str) -> Occupancy | None:
    return seats.watch(pipe, seat) if pipe is not None else seats.occupant(seat)


def recipient(value: str | Recipient) -> tuple[str, str]:
    from scripts.inbox.store import InboxError

    if isinstance(value, str):
        return value, ""
    if not value.execution:
        raise InboxError("an agent bound order needs its recipient execution")
    return value.name, value.execution


def recipient_for(store: "RedisStore", name: str) -> Recipient:
    name, slug = _address(store.names, name)
    row = fencing.records(store, slug).record(slug, fencing.BY_NAME, name)
    return Recipient(name, str(row.value) if row is not None else "")


def expired(inbox: "InboxStore", item: "Item", pipe: Any = None) -> bool:
    from scripts.swarm.store import RedisStore

    if not item.execution:
        return False
    address, slug = _address(inbox.names, item.address)
    store = RedisStore(inbox.redis)
    if pipe is not None:
        fencing.watch(store, pipe)
    records = fencing.records(store, slug)
    row = records.record(slug, "executions", item.execution)
    if row is None:
        return True
    payload: Any = row.value
    value = dict[str, Any](payload)
    seat = str(value["seat"])
    name = str(value["name"])
    if value["state"] in TERMINAL_STATES or (item.address != seat and address != name):
        return True
    head = records.record(slug, fencing.BY_SEAT, seat)
    if head is None or head.value != item.execution:
        return True
    occupant = _occupant(inbox.seats, pipe, seat)
    return occupant is None or occupant.occupant != name or inbox.seats.left(name, seat)


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
    for address in (name, held) if held else (name,):
        for item in inbox.open_items(address):
            if not item.execution:
                continue
            _, slug = _address(inbox.names, name)
            records = fencing.records(RedisStore(inbox.redis), slug)
            row = records.record(slug, "executions", item.execution) or records.record(slug, "archive", item.execution)
            if row is None:
                continue
            payload: Any = row.value
            if dict[str, Any](payload)["name"] == name:
                inbox.settle(item.id, item.sender, "done", ENDED)
