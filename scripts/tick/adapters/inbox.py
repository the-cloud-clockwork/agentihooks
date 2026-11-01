"""Inbox port adapter: the tick's inbox tools over the durable inbox store, each call admitted only while its deadline holds."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from scripts.tick.deadline import Deadline

if TYPE_CHECKING:
    from scripts.inbox.store import InboxStore

__all__ = ["StoreInbox", "check"]


def check(deadline: Deadline) -> None:
    """Refuse a tool call whose deadline has already passed."""
    if deadline.expired:
        raise TimeoutError("tool deadline expired")


@dataclass(frozen=True)
class StoreInbox:
    store: "InboxStore"
    sender: str

    def read(self, address: str, *, deadline: Deadline) -> object:
        check(deadline)
        return self.store.inbox(address)

    def send(self, address: str, body: str, *, deadline: Deadline) -> object:
        check(deadline)
        return self.store.send(self.sender, address, body)

    def close(self, item_id: str, *, deadline: Deadline) -> object:
        check(deadline)
        return self.store.close(item_id, self.sender, "done")
