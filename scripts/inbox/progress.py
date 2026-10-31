"""Retained channel progress and an expiring lease renewed by successful polls."""

import json
import uuid
from dataclasses import dataclass, field

from scripts.inbox.store import InboxStore, now_ms

LEASE_S = 15


@dataclass
class ChannelProgress:
    store: InboxStore
    recipient: str
    owner: str = field(default_factory=lambda: uuid.uuid4().hex)
    ready_at: int = field(default_factory=now_ms)
    poll_at: int = 0
    delivery_at: int = 0

    def save(self) -> None:
        record = {
            "owner": self.owner,
            "ready_at": self.ready_at,
            "poll_at": self.poll_at,
            "delivery_at": self.delivery_at,
        }
        self.store.redis.set(self.store.key("channel-progress", self.recipient), json.dumps(record))

    def delivered(self) -> None:
        self.delivery_at = now_ms()
        self.save()

    def polled(self) -> None:
        self.poll_at = now_ms()
        self.save()
        self.store.redis.set(self.store.key("channel-live", self.recipient), self.owner, ex=LEASE_S)


def healthy(store: InboxStore, recipient: str) -> bool:
    return bool(store.redis.get(store.key("channel-live", recipient)))
