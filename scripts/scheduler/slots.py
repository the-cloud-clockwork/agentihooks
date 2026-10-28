import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from importlib import import_module
from types import FunctionType
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from scripts.events.bus import StateStore


class Slot(Protocol):
    def record(self) -> dict: ...

    def deliver(self, event: dict, delivery_id: str, state: "StateStore") -> None:
        """Commit effects idempotently under delivery_id; retries reuse that identity."""
        ...


@dataclass(frozen=True)
class CallableSlot:
    reference: str

    @classmethod
    def from_callback(cls, callback: Callable) -> "CallableSlot":
        if (
            not isinstance(callback, FunctionType)
            or callback.__module__ == "__main__"
            or not callback.__qualname__.isidentifier()
        ):
            raise ValueError("a slot must be an importable module function")
        return cls(f"{callback.__module__}:{callback.__qualname__}")

    def record(self) -> dict:
        return {"kind": "callable", **asdict(self)}

    def deliver(self, event: dict, delivery_id: str, state: "StateStore") -> None:
        module, name = self.reference.split(":")
        getattr(import_module(module), name)(event, delivery_id, state)


def _legacy(state: "StateStore"):
    from scripts.events.redis_state import RedisStateStore

    return state.redis if isinstance(state, RedisStateStore) else None


def _inbox(state: "StateStore"):
    from scripts.inbox import store

    redis = _legacy(state)
    return store.connect() if redis is None else store.InboxStore(redis)


def swarm_store(state: "StateStore"):
    from scripts.swarm import store

    redis = _legacy(state)
    return store.connect() if redis is None else store.RedisStore(redis)


@dataclass(frozen=True)
class InboxSlot:
    address: str

    def record(self) -> dict:
        return {"kind": "inbox", **asdict(self)}

    def deliver(self, event: dict, delivery_id: str, state: "StateStore") -> None:
        from scripts.inbox.store import DuplicateItem

        try:
            _inbox(state).send(event["owner"], self.address, event["payload"], item_id=delivery_id)
        except DuplicateItem:
            pass


@dataclass(frozen=True)
class BroadcastSlot:
    channel: str | None = None

    def record(self) -> dict:
        return {"kind": "broadcast", **asdict(self)}

    def deliver(self, event: dict, delivery_id: str, state: "StateStore") -> None:
        from hooks.context.broadcast import create_broadcast

        create_broadcast(event["payload"], source=event["owner"], channel=self.channel, delivery_id=delivery_id)


@dataclass(frozen=True)
class SwarmSlot:
    slug: str

    def record(self) -> dict:
        return {"kind": "swarm", **asdict(self)}

    def deliver(self, event: dict, delivery_id: str, state: "StateStore") -> None:
        from scripts.inbox.outbox import stable_id
        from scripts.inbox.store import DuplicateItem
        from scripts.swarm.delivery import recipients

        inbox = _inbox(state)
        for agent in recipients(swarm_store(state), self.slug, "all", event["owner"]):
            try:
                inbox.send(
                    event["owner"],
                    agent.name,
                    event["payload"],
                    task=agent.task,
                    item_id=stable_id(delivery_id, agent.name),
                )
            except DuplicateItem:
                pass


KINDS = {"inbox": InboxSlot, "broadcast": BroadcastSlot, "swarm": SwarmSlot, "callable": CallableSlot}


def restore(record: dict) -> Slot:
    fields = dict(record)
    kind = fields.pop("kind")
    return KINDS[kind](**fields)


def first_delivery(state: "StateStore", owner: str, delivery_id: str) -> bool:
    from scripts.events.bus import StateConflict

    key = "delivered." + hashlib.sha256(json.dumps([owner, delivery_id]).encode()).hexdigest()
    try:
        state.create(key, delivery_id.encode())
    except StateConflict:
        entry = state.get(key)
        if entry is not None and entry.value == delivery_id.encode():
            return False
        raise
    return True
