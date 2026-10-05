"""Durable agent inbox in Redis: message items indexed by address, each with an append-only history.

History entries are state transitions (a `state` key) or wake and escalation steps (an `event` key).
"""

import json
import time
import uuid
from dataclasses import asdict, dataclass, replace

PREFIX = "agentihooks:inbox"
STATES = ("pending", "delivered", "read", "done", "blocked", "handed_off", "cancelled")
CLOSED = ("done", "blocked", "handed_off", "cancelled")
CLOSE_KINDS = {"done": "done", "handoff": "handed_off", "blocked": "blocked", "cancel": "cancelled"}
NEEDS_DETAIL = {"handoff": "the address the work went to", "blocked": "what it is blocked on"}


class InboxError(RuntimeError):
    pass


@dataclass(frozen=True)
class Item:
    id: str
    sender: str
    address: str
    text: str
    state: str
    created_at: int
    updated_at: int
    reason: str = ""


def now_ms():
    return time.time_ns() // 1_000_000


def close_reason(kind, detail=""):
    detail = detail.strip()
    if kind not in CLOSE_KINDS:
        raise InboxError(f"closing needs a reason: one of {', '.join(CLOSE_KINDS)}")
    if kind in NEEDS_DETAIL and not detail:
        raise InboxError(f"{kind} needs {NEEDS_DETAIL[kind]}")
    if kind == "handoff":
        return CLOSE_KINDS[kind], f"handed off to {detail}"
    if kind == "blocked":
        return CLOSE_KINDS[kind], f"blocked on {detail}"
    return CLOSE_KINDS[kind], CLOSE_KINDS[kind] + (f": {detail}" if detail else "")


class InboxStore:
    def __init__(self, redis):
        if redis is None:
            raise InboxError("no Redis client; the inbox refuses to run without it")
        self.redis = redis

    def key(self, *parts):
        return ":".join((PREFIX, *parts))

    def send(self, sender, address, text):
        if not (sender and address and text.strip()):
            raise InboxError("a message needs a sender, an address and text")
        at = now_ms()
        item = Item(uuid.uuid4().hex[:12], sender, address, text, "pending", at, at)
        with self.redis.pipeline() as pipe:
            pipe.hset(self.key("item", item.id), mapping=_fields(item))
            pipe.zadd(self.key("address", address), {item.id: at})
            pipe.rpush(self.key("history", item.id), _entry("pending", sender, "", at))
            pipe.execute()
        return item

    def get(self, item_id):
        return _item(self.redis.hgetall(self.key("item", item_id)), item_id)

    def inbox(self, address):
        return [self.get(item_id) for item_id in self.redis.zrange(self.key("address", address), 0, -1)]

    def history(self, item_id):
        return [json.loads(entry) for entry in self.redis.lrange(self.key("history", item_id), 0, -1)]

    def read(self, item_id, reader):
        return self._move(item_id, reader, lambda item: (item.address,), "read", "")

    def close(self, item_id, closer, kind, detail=""):
        state, reason = close_reason(kind, detail)
        return self._move(item_id, closer, lambda item: (item.address, item.sender), state, reason)

    def deliver(self, item_id, receiver):
        try:
            return self._move(item_id, receiver, lambda item: (item.address,), "delivered", "", only_from=("pending",))
        except InboxError:
            return None

    def pending(self):
        addresses = self.redis.scan_iter(match=self.key("address", "*"))
        items = [self.get(i) for key in addresses for i in self.redis.zrange(key, 0, -1)]
        return [item for item in items if item.state == "pending"]

    def note(self, item_id, event, by, detail, at):
        self.redis.rpush(
            self.key("history", item_id), json.dumps({"event": event, "by": by, "reason": detail, "at": at})
        )

    def _move(self, item_id, by, actors, state, reason, only_from=None):
        from redis.exceptions import WatchError

        key = self.key("item", item_id)
        with self.redis.pipeline() as pipe:
            try:
                pipe.watch(key)
                item = _item(pipe.hgetall(key), item_id)
                if by not in actors(item):
                    raise InboxError(f"message {item_id} belongs to {item.address}, not {by}")
                if only_from is not None and item.state not in only_from:
                    return None
                if item.state in CLOSED:
                    raise InboxError(f"message {item_id} is closed: {item.reason}")
                if item.state == state:
                    return item
                moved = replace(item, state=state, updated_at=now_ms(), reason=reason)
                pipe.multi()
                pipe.hset(key, mapping=_fields(moved))
                pipe.rpush(self.key("history", item_id), _entry(state, by, reason, moved.updated_at))
                pipe.execute()
                return moved
            except WatchError as exc:
                raise InboxError(f"message {item_id} changed meanwhile; run the command again") from exc


def _fields(item):
    return {k: str(v) for k, v in asdict(item).items()}


def _item(raw, item_id):
    if not raw:
        raise InboxError(f"no message {item_id}")
    return Item(**{**raw, "created_at": int(raw["created_at"]), "updated_at": int(raw["updated_at"])})


def _entry(state, by, reason, at):
    return json.dumps({"state": state, "by": by, "reason": reason, "at": at})


def connect(environ=None):
    import redis

    from scripts.swarm.store import redis_client

    try:
        return InboxStore(redis_client(environ))
    except redis.RedisError as exc:
        raise InboxError(f"Redis is unreachable ({exc}); the inbox refuses to run without it") from exc
