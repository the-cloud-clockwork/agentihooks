"""Durable agent inbox in Redis: message items indexed by address, each with an append-only history.

History entries are state transitions (a `state` key) or wake and escalation steps (an `event` key).
"""

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, fields, replace

from scripts.inbox.seats import SeatRegistry, is_seat, master_of
from scripts.swarm.keyspace import ROOT
from scripts.swarm.naming import NameRegistry

PREFIX = f"{ROOT}:inbox"
NOTIFY = f"{PREFIX}:notify"
MOVE_ATTEMPTS = 3
STATES = ("pending", "delivered", "confirmed", "read", "done", "blocked", "handed_off", "cancelled")
REDELIVER_ENV = "AGENTIHOOKS_INBOX_REDELIVER_S"
DEFAULT_REDELIVER_S = 300
OWNER_TTL_ENV = "AGENTIHOOKS_INBOX_OWNER_TTL_S"
DEFAULT_OWNER_TTL_S = 30
REDELIVERED = "redelivered: never confirmed inside the redelivery window"
REDELIVERER = "inbox"
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
    ref: str = ""
    sequence: int = 0
    fyi: bool = False
    task: str = ""


def now_ms():
    return time.time_ns() // 1_000_000


def owner_key(recipient):
    return f"{PREFIX}:owner:{recipient}"


def owner_ttl_s(environ=None):
    env = os.environ if environ is None else environ
    return int(env.get(OWNER_TTL_ENV) or DEFAULT_OWNER_TTL_S)


def redelivery_ms(environ=None):
    env = os.environ if environ is None else environ
    return int(env.get(REDELIVER_ENV) or DEFAULT_REDELIVER_S) * 1000


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
        self.seats = SeatRegistry(redis)
        self.names = NameRegistry(redis)

    def key(self, *parts):
        return ":".join((PREFIX, *parts))

    def receiver_task(self, address: str) -> str:
        from scripts.inbox.seats import is_seat, master_of
        from scripts.swarm.store import MASTER, RedisStore

        address = self.names.resolve(address)
        master = master_of(address, self.names)
        if not master:
            return ""
        receiver = self.seats.occupant(address).occupant if is_seat(address) else address
        return next(
            (
                agent.task
                for agent in RedisStore(self.redis).agents(master.split("@", 1)[1])
                if agent.name == receiver and agent.state != "finished" and agent.task != MASTER
            ),
            "",
        )

    def send(self, sender, address, text, ref="", fyi=False, task=""):
        """ref names the ledger write an operator item carries, for the seen marks; fyi marks an item that needs no
        work, so a bare close names no outcome."""
        if not (sender and address and text.strip()):
            raise InboxError("a message needs a sender, an address and text")
        sender, address = self.names.resolve(sender), self.names.resolve(address)
        at = now_ms()
        item = Item(
            uuid.uuid4().hex[:12],
            sender,
            address,
            text,
            "pending",
            at,
            at,
            ref=ref,
            sequence=self.redis.incr(self.key("sequence", address)),
            fyi=fyi,
            task=task,
        )
        with self.redis.pipeline() as pipe:
            pipe.hset(self.key("item", item.id), mapping=_fields(item))
            pipe.zadd(self.key("address", address), {item.id: at})
            pipe.zadd(self.key("pending", address), {item.id: at})
            pipe.zadd(self.key("open", address), {item.id: at})
            pipe.incr(self.key("open-size", address))
            pipe.sadd(self.key("waiting"), address)
            pipe.rpush(self.key("history", item.id), _entry("pending", sender, "", at))
            pipe.publish(NOTIFY, address)
            pipe.execute()
        return item

    def get(self, item_id):
        return _item(self.redis.hgetall(self.key("item", item_id)), item_id)

    def inbox(self, address):
        items = [self.get(item_id) for item_id in self.redis.zrange(self.key("address", address), 0, -1)]
        return sorted(items, key=_order)

    def open_items(self, address: str) -> list[Item]:
        items = [self.get(item_id) for item_id in self._open_ids(address)]
        closed = [item.id for item in items if item.state in CLOSED]
        if closed:
            self.redis.zrem(self.key("open", address), *closed)
        return sorted((item for item in items if item.state not in CLOSED), key=_order)

    def quiet(self, addresses: list[str]) -> set[str]:
        """Addresses whose open index is current and holds nothing; any other needs open_items to tell."""
        with self.redis.pipeline(transaction=False) as pipe:
            for address in addresses:
                pipe.sismember(self.key("open-indexed"), address)
                pipe.get(self.key("open-size", address))
                pipe.zcard(self.key("address", address))
                pipe.zcard(self.key("open", address))
            rows = pipe.execute()
        return {
            address
            for address, (indexed, size, total, opened) in zip(addresses, zip(*[iter(rows)] * 4))
            if _index_current(indexed, size, total) and not opened
        }

    def _open_ids(self, address):
        from redis.exceptions import WatchError

        if _index_current(
            self.redis.sismember(self.key("open-indexed"), address),
            self.redis.get(self.key("open-size", address)),
            self.redis.zcard(self.key("address", address)),
        ):
            return self.redis.zrange(self.key("open", address), 0, -1)
        for _ in range(MOVE_ATTEMPTS):
            try:
                return self._index_open(address)
            except WatchError:
                continue
        return [item.id for item in self.inbox(address) if item.state not in CLOSED]

    def _index_open(self, address):
        with self.redis.pipeline() as pipe:
            pipe.watch(self.key("address", address))
            ids = pipe.zrange(self.key("address", address), 0, -1)
            if ids:
                pipe.watch(*(self.key("item", item_id) for item_id in ids))
            items = [_item(pipe.hgetall(self.key("item", item_id)), item_id) for item_id in ids]
            opened = {item.id: item.created_at for item in items if item.state not in CLOSED}
            pipe.multi()
            pipe.delete(self.key("open", address))
            if opened:
                pipe.zadd(self.key("open", address), opened)
            pipe.sadd(self.key("open-indexed"), address)
            pipe.set(self.key("open-size", address), len(ids))
            pipe.execute()
        return list(opened)

    def mailbox(self, me):
        return self._with_seat(me, self.inbox)

    def pending_mail(self, me):
        return self._with_seat(me, self.pending_items)

    def _with_seat(self, me, read):
        from redis.exceptions import WatchError

        original = me
        for _ in range(MOVE_ATTEMPTS):
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(self.names.key("alias", original))
                    me = self.names.resolve(original, pipe)
                    pipe.watch(self.names.key("aliases-of", me))
                    addresses = [me, *self.names.aliases(me, pipe)]
                    seat = self.seats.seat_of(me, pipe)
                    pipe.multi()
                    pipe.ping()
                    pipe.execute()
                    break
                except WatchError:
                    continue
        else:
            raise InboxError(f"aliases for {original} changed meanwhile")
        if seat and not self.seats.left(me, seat):
            addresses.append(seat)
        items = [item for address in addresses for item in read(address)]
        return sorted({item.id: item for item in items}.values(), key=_order)

    def acts_for(self, by, address, pipe=None):
        by, address = self.names.resolve(by, pipe), self.names.resolve(address, pipe)
        if by == address:
            return True
        if not is_seat(address):
            return False
        held = self.seats.watch(pipe, address) if pipe is not None else self.seats.occupant(address)
        return self.names.resolve(held.occupant, pipe) == by

    def history(self, item_id):
        return [json.loads(entry) for entry in self.redis.lrange(self.key("history", item_id), 0, -1)]

    def keys_for(self, belongs):
        """The keys of every address belongs() accepts, its items and their histories, and those
        addresses' memberships in the shared waiting and indexed sets."""
        prefix = self.key("address", "")
        seen = {key[len(prefix) :] for key in self.redis.scan_iter(match=prefix + "*")}
        seen.update(self.redis.smembers(self.key("open-indexed")))
        size_prefix = self.key("open-size", "")
        seen.update(key[len(size_prefix) :] for key in self.redis.scan_iter(match=size_prefix + "*"))
        addresses = sorted(a for a in seen if belongs(a))
        keys = []
        for address in addresses:
            ids = self.redis.zrange(self.key("address", address), 0, -1)
            keys += [self.key(kind, address) for kind in ("address", "pending", "open", "open-size", "sequence")]
            keys += [self.key(kind, item_id) for item_id in ids for kind in ("item", "history")]
        members = {
            self.key(shared): [a for a in addresses if self.redis.sismember(self.key(shared), a)]
            for shared in ("waiting", "indexed", "open-indexed")
        }
        return keys, {key: found for key, found in members.items() if found}

    def read(self, item_id, reader):
        return self._move(item_id, reader, lambda item: (item.address,), "read", "")

    def close(self, item_id, closer, kind, detail=""):
        state, reason = close_reason(kind, detail)
        return self._move(
            item_id,
            closer,
            lambda item: (item.address, item.sender, master_of(item.address, NameRegistry(self.redis))),
            state,
            reason,
        )

    def withdraw(
        self, item_id: str, by: str, reason: str, expected_address: str = "", expected_receiver: str = ""
    ) -> Item | None:
        return self.redirect(item_id, by, "", reason, expected_address, expected_receiver)

    def redirect(
        self,
        item_id: str,
        by: str,
        address: str,
        reason: str,
        expected_address: str = "",
        expected_receiver: str = "",
    ) -> Item | None:
        """Return an open item to pending at another address; a swarm step, so no actor check."""
        from redis.exceptions import WatchError

        for _ in range(MOVE_ATTEMPTS):
            try:
                return self._try_redirect(item_id, by, address, reason, expected_address, expected_receiver)
            except WatchError:
                continue
        raise InboxError(f"message {item_id} changed meanwhile; run the command again")

    def _try_redirect(self, item_id, by, address, reason, expected_address, expected_receiver):
        key = self.key("item", item_id)
        with self.redis.pipeline() as pipe:
            pipe.watch(key)
            item = _item(pipe.hgetall(key), item_id)
            if item.state in CLOSED or (expected_address and item.address != expected_address):
                return None
            if expected_receiver:
                history = self.key("history", item_id)
                pipe.watch(history)
                entries = [json.loads(entry) for entry in pipe.lrange(history, 0, -1)]
                receiver = next(
                    (entry["by"] for entry in reversed(entries) if entry["state"] in ("delivered", "read")), ""
                )
                if receiver != expected_receiver:
                    return None
            pending = self.key("pending", item.address)
            pipe.watch(pending)
            last = pipe.zscore(pending, item_id) is not None and pipe.zcard(pending) == 1
            moved = replace(
                item,
                address=address or item.address,
                state="pending" if address else "cancelled",
                updated_at=now_ms(),
                reason=reason,
            )
            pipe.multi()
            pipe.hset(key, mapping=_fields(moved))
            if moved.state in CLOSED or moved.address != item.address:
                pipe.zrem(self.key("open", item.address), item_id)
            pipe.zrem(self.key("delivered"), item_id)
            pipe.zrem(pending, item_id)
            if last:
                pipe.srem(self.key("waiting"), item.address)
            if address:
                pipe.zrem(self.key("address", item.address), item_id)
                pipe.zadd(self.key("address", address), {item_id: item.created_at})
                pipe.zadd(self.key("pending", address), {item_id: item.created_at})
                pipe.zadd(self.key("open", address), {item_id: item.created_at})
                if address != item.address:
                    pipe.decr(self.key("open-size", item.address))
                    pipe.incr(self.key("open-size", address))
                pipe.sadd(self.key("waiting"), address)
                pipe.publish(NOTIFY, address)
            pipe.rpush(self.key("history", item_id), _entry(moved.state, by, reason, moved.updated_at))
            pipe.execute()
            return moved

    def reply(self, item_id, replier, text, fyi=False):
        item = self.get(item_id)
        if not self.acts_for(replier, item.address):
            raise InboxError(f"message {item_id} belongs to {item.address}, not {replier}")
        if item.state in CLOSED:
            raise InboxError(f"message {item_id} is closed: {item.reason}")
        answer = self.send(replier, item.sender, text, fyi=fyi)
        self.close(item_id, replier, "done", f"replied with message {answer.id}")
        return answer

    def deliver(self, item_id, receiver):
        try:
            return self._move(item_id, receiver, lambda item: (item.address,), "delivered", "", only_from=("pending",))
        except InboxError:
            return None

    def confirm(self, item_id, receiver):
        return self._move(
            item_id, receiver, lambda item: (item.address,), "confirmed", "", only_from=("delivered", "read")
        )

    def confirm_shown(self, receiver, before):
        """Confirm every item delivered to receiver before this hook event began: the session lived past it."""
        receiver = self.names.resolve(receiver)
        confirmed = []
        for item_id in self.redis.zrangebyscore(self.key("delivered"), "-inf", before - 1):
            if self.names.resolve(self._delivered_to(item_id)) != receiver:
                continue
            try:
                item = self.confirm(item_id, receiver)
            except InboxError:
                continue
            if item is not None:
                confirmed.append(item)
        return confirmed

    def _delivered_to(self, item_id):
        return next((e["by"] for e in reversed(self.history(item_id)) if e.get("state") == "delivered"), "")

    def redeliver(self, now, window):
        """Return each item delivered at least window ms ago and never confirmed to pending, for the next claim."""
        cutoff = now - window
        with self.redis.pipeline() as pipe:
            pipe.exists(self.key("delivered", "built"))
            pipe.zrangebyscore(self.key("delivered"), "-inf", cutoff)
            built, stale = pipe.execute()
        if not built:
            self._build_delivered(cutoff)
        return [item for item_id in stale if (item := self.requeue(item_id, REDELIVERER, REDELIVERED, cutoff))]

    def _build_delivered(self, since):
        """Index the items a store without the delivered index left delivered after since; older ones stay as they are."""
        prefix = self.key("item", "")
        for key in self.redis.scan_iter(match=prefix + "*"):
            state, updated_at = self.redis.hmget(key, "state", "updated_at")
            if state == "delivered" and int(updated_at) > since:
                self.redis.zadd(self.key("delivered"), {key[len(prefix) :]: int(updated_at)})
        self.redis.set(self.key("delivered", "built"), 1)

    def requeue(self, item_id, by, reason, before=None):
        """Return a delivered item to pending; None once it moved on, or was delivered again after before."""
        from redis.exceptions import WatchError

        for _ in range(MOVE_ATTEMPTS):
            try:
                return self._try_requeue(item_id, by, reason, before)
            except WatchError:
                continue
        raise InboxError(f"message {item_id} changed meanwhile; run the command again")

    def _try_requeue(self, item_id, by, reason, before):
        key = self.key("item", item_id)
        with self.redis.pipeline() as pipe:
            pipe.watch(key)
            raw = pipe.hgetall(key)
            if raw and raw["state"] == "delivered" and before is not None and int(raw["updated_at"]) > before:
                return None
            pipe.multi()
            pipe.zrem(self.key("delivered"), item_id)
            if not raw or raw["state"] != "delivered":
                pipe.execute()
                return None
            item = _item(raw, item_id)
            moved = replace(item, state="pending", updated_at=now_ms(), reason=reason)
            pipe.hset(key, mapping=_fields(moved))
            pipe.zadd(self.key("pending", item.address), {item_id: item.created_at})
            pipe.sadd(self.key("waiting"), item.address)
            pipe.rpush(self.key("history", item_id), _entry("pending", by, reason, moved.updated_at))
            pipe.publish(NOTIFY, item.address)
            pipe.execute()
            return moved

    def pending(self):
        if not self.redis.exists(self.key("waiting", "built")):
            self._build_waiting()
        addresses = sorted(self.redis.smembers(self.key("waiting")))
        items = list({item.id: item for address in addresses for item in self.pending_items(address)}.values())
        return sorted(items, key=_order)

    def pending_items(self, address):
        items = [self.get(item_id) for item_id in self._pending_ids(address)]
        return sorted(items, key=_order)

    def _pending_ids(self, address):
        if self.redis.sismember(self.key("indexed"), address):
            return self.redis.zrange(self.key("pending", address), 0, -1)
        return self._back_fill(address)

    def _build_waiting(self):
        prefix = self.key("address", "")
        for key in self.redis.scan_iter(match=prefix + "*"):
            address = key[len(prefix) :]
            self._pending_ids(address)
            self._mark_waiting(address)
        self.redis.set(self.key("waiting", "built"), 1)

    def _mark_waiting(self, address):
        from redis.exceptions import WatchError

        pending = self.key("pending", address)
        for _ in range(MOVE_ATTEMPTS):
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(pending)
                    if not pipe.zcard(pending):
                        return
                    pipe.multi()
                    pipe.sadd(self.key("waiting"), address)
                    pipe.execute()
                    return
                except WatchError:
                    continue
        self.redis.sadd(self.key("waiting"), address)

    def _back_fill(self, address):
        from redis.exceptions import WatchError

        for _ in range(MOVE_ATTEMPTS):
            try:
                return self._try_back_fill(address)
            except WatchError:
                continue
        return [item.id for item in self.inbox(address) if item.state == "pending"]

    def _try_back_fill(self, address):
        with self.redis.pipeline() as pipe:
            pipe.watch(self.key("address", address))
            ids = pipe.zrange(self.key("address", address), 0, -1)
            if ids:
                pipe.watch(*(self.key("item", item_id) for item_id in ids))
            items = [_item(pipe.hgetall(self.key("item", item_id)), item_id) for item_id in ids]
            pending = {item.id: item.created_at for item in items if item.state == "pending"}
            pipe.multi()
            if pending:
                pipe.zadd(self.key("pending", address), pending)
                pipe.sadd(self.key("waiting"), address)
            pipe.sadd(self.key("indexed"), address)
            pipe.execute()
        return list(pending)

    def note(self, item_id, event, by, detail, at, held=None):
        """held=(seat address, generation): write nothing and return False once that seat has a new occupant."""
        from redis.exceptions import WatchError

        entry = json.dumps({"event": event, "by": by, "reason": detail, "at": at})
        with self.redis.pipeline() as pipe:
            try:
                if held is not None and self.seats.watch(pipe, held[0]).generation != held[1]:
                    return False
                pipe.multi()
                pipe.rpush(self.key("history", item_id), entry)
                pipe.execute()
                return True
            except WatchError:
                return False

    def _move(self, item_id, by, actors, state, reason, only_from=None):
        from redis.exceptions import WatchError

        for _ in range(MOVE_ATTEMPTS):
            try:
                return self._try_move(item_id, by, actors, state, reason, only_from)
            except WatchError:
                continue
        raise InboxError(f"message {item_id} changed meanwhile; run the command again")

    def _try_move(self, item_id, by, actors, state, reason, only_from):
        key = self.key("item", item_id)
        with self.redis.pipeline() as pipe:
            pipe.watch(key)
            item = _item(pipe.hgetall(key), item_id)
            if not any(self.acts_for(by, address, pipe) for address in actors(item)):
                raise InboxError(f"message {item_id} belongs to {item.address}, not {by}")
            if only_from is not None and item.state not in only_from:
                return None
            if item.state in CLOSED:
                raise InboxError(f"message {item_id} is closed: {item.reason}")
            if state == "done" and not item.fyi and reason == "done":
                raise InboxError("done needs an outcome naming where the work went")
            if item.state == state:
                return item
            if state == "delivered" and self._owned(pipe, by):
                return None
            moved = replace(item, state=state, updated_at=now_ms(), reason=reason)
            last = self.last_pending(pipe, item)
            pipe.multi()
            self.stage_move(pipe, item, moved, by, last)
            pipe.execute()
            return moved

    def _owned(self, pipe, by):
        key = owner_key(NameRegistry(pipe).resolve(by))
        pipe.watch(key)
        return pipe.get(key) is not None

    def last_pending(self, pipe, item):
        pending = self.key("pending", item.address)
        pipe.watch(pending)
        return pipe.zscore(pending, item.id) is not None and pipe.zcard(pending) == 1

    def stage_move(self, pipe, item, moved, by, last, indexed=True):
        """pipe is already in MULTI; indexed=False keeps a delivered item out of the redelivery index."""
        pipe.hset(self.key("item", item.id), mapping=_fields(moved))
        if moved.state in CLOSED:
            pipe.zrem(self.key("open", item.address), item.id)
        if moved.state == "delivered" and indexed:
            pipe.zadd(self.key("delivered"), {item.id: moved.updated_at})
        else:
            pipe.zrem(self.key("delivered"), item.id)
        pipe.zrem(self.key("pending", item.address), item.id)
        if last:
            pipe.srem(self.key("waiting"), item.address)
        pipe.rpush(self.key("history", item.id), _entry(moved.state, by, moved.reason, moved.updated_at))


def _index_current(indexed, size, total: int) -> bool:
    return bool(indexed) and size is not None and int(size) == total


def _fields(item):
    return {k: str(v) for k, v in asdict(item).items()}


def _order(item: Item) -> tuple[int, int, str]:
    return item.created_at, item.sequence, item.id


def _item(raw, item_id):
    if not raw:
        raise InboxError(f"no message {item_id}")
    known = {field.name for field in fields(Item)}
    return Item(
        **{
            **{key: value for key, value in raw.items() if key in known},
            "created_at": int(raw["created_at"]),
            "updated_at": int(raw["updated_at"]),
            "sequence": int(raw.get("sequence", 0)),
            "fyi": raw.get("fyi") == "True",
        }
    )


def _entry(state, by, reason, at):
    return json.dumps({"state": state, "by": by, "reason": reason, "at": at})


def connect(environ=None):
    import redis

    from scripts.swarm.store import redis_client

    try:
        return InboxStore(redis_client(environ))
    except redis.RedisError as exc:
        raise InboxError(f"Redis is unreachable ({exc}); the inbox refuses to run without it") from exc
