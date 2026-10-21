"""Durable agent inbox in Redis: message items indexed by address, each with an append-only history.

History entries are state transitions (a `state` key) or wake and escalation steps (an `event` key).
"""

import functools
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
RECLAIMED = "returned to the master pool: its master is gone"
CLOSED = ("done", "blocked", "handed_off", "cancelled")
CLOSE_KINDS = {"done": "done", "handoff": "handed_off", "blocked": "blocked", "cancel": "cancelled"}
NEEDS_DETAIL = {"handoff": "the address the work went to", "blocked": "what it is blocked on"}
POOL_HOLDER = "pool_holder"
POOL_SKIP = "pool_skip"


class InboxError(RuntimeError):
    pass


class DuplicateItem(InboxError):
    pass


class Unreachable(InboxError):
    pass


class Contended(InboxError):
    pass


def new_item_id():
    return uuid.uuid4().hex[:12]


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
    delivery_session: str = ""


def now_ms():
    return time.time_ns() // 1_000_000


def prefix(world):
    from scripts.hive import scope

    return f"{scope.root(world)}:inbox"


def owner_key(recipient, world=None):
    """world is the inbox's hive member; None takes this process's own."""
    from scripts.hive import scope

    return f"{prefix(scope.member() if world is None else world)}:owner:{recipient}"


def owner_ttl_s(environ=None):
    env = os.environ if environ is None else environ
    return max(1, int(env.get(OWNER_TTL_ENV) or DEFAULT_OWNER_TTL_S))


def redelivery_ms(environ=None):
    env = os.environ if environ is None else environ
    return int(env.get(REDELIVER_ENV) or DEFAULT_REDELIVER_S) * 1000


def replied(answer_id):
    return f"replied with message {answer_id}"


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
    def __init__(self, redis, *, persist=None, session_id="", world=None):
        """world is the hive member whose keys hold this inbox, '' for the controller's; it defaults to this process's
        own member."""
        from scripts.hive import scope

        if redis is None:
            raise InboxError("no Redis client; the inbox refuses to run without it")
        self.redis = redis
        self.persist = persist
        self.session_id = session_id
        self.seats = SeatRegistry(redis)
        self.names = NameRegistry(redis)
        self.world = scope.member() if world is None else world
        self.prefix = prefix(self.world)

    def key(self, *parts):
        return ":".join((self.prefix, *parts))

    def home(self, address):
        """The hive member whose keys hold address's inbox: the member that runs it or its seat's occupant."""
        from scripts.hive import scope

        if not scope.joined(self.redis):
            return ""
        receiver = self.names.resolve(self.seats.occupant(address).occupant if is_seat(address) else address)
        slug = self.names.slug_of(receiver)
        return scope.host(self.redis, slug, receiver) if slug else ""

    def worlds(self) -> list["InboxStore"]:
        """This inbox and, for the controller's, each joined hive member's, which holds the mail of the agents that
        member runs."""
        from scripts.hive import scope

        if self.world or scope.member():
            return [self]
        return [self, *(InboxStore(self.redis, persist=self.persist, world=m) for m in scope.members(self.redis))]

    @functools.cached_property
    def pool(self):
        from scripts.inbox.pool import MasterPool

        return MasterPool(self)

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

    def send(self, sender, address, text, ref="", fyi=False, task="", item_id=""):
        """ref names the ledger write an operator item carries, for the seen marks; fyi marks an item that needs no
        work, so a bare close names no outcome; a given item_id refuses a second write of that item."""
        if not (sender and address and text.strip()):
            raise InboxError("a message needs a sender, an address and text")
        from scripts.hive import scope

        sender, address = self.names.resolve(sender), self.names.resolve(address)
        home = self.home(address)
        if home != self.world and not scope.member():
            routed = InboxStore(self.redis, persist=self.persist, world=home)
            return routed.send(sender, address, text, ref, fyi, task, item_id)
        at = now_ms()
        item = Item(
            item_id or new_item_id(),
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
        key = self.key("item", item.id)
        masters = self.pool.masters_for(sender, address) if home == self.world else []
        with self.redis.pipeline() as pipe:
            if item_id:
                pipe.watch(key)
                if pipe.exists(key):
                    raise DuplicateItem(f"message {item.id} was already sent")
                pipe.multi()
            pipe.hset(key, mapping=_fields(item))
            pipe.zadd(self.key("address", address), {item.id: at})
            pipe.zadd(self.key("open", address), {item.id: at})
            pipe.incr(self.key("open-size", address))
            if masters:
                self.pool.stage_add(pipe, item)
            else:
                pipe.zadd(self.key("pending", address), {item.id: at})
                pipe.sadd(self.key("waiting"), address)
            pipe.rpush(self.key("history", item.id), _entry("pending", sender, "", at))
            if self.persist is not None:
                self.persist(pipe)
            pipe.publish(NOTIFY, address)
            pipe.execute()
        if masters:
            from scripts.inbox.pool import slug_of

            self.pool.distribute(slug_of(address))
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

    def open_mail(self, receiver: str) -> list[Item]:
        return self._with_seat(receiver, self.open_items)

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

    def unsettled(self, receiver: str, before: int) -> list[Item]:
        return [
            item
            for item in self._with_seat(receiver, lambda address: self._due_open(address, before))
            if not item.fyi and item.state in ("read", "confirmed") and item.updated_at <= before
        ]

    def _due_open(self, address, before):
        with self.redis.pipeline() as pipe:
            pipe.sismember(self.key("open-indexed"), address)
            pipe.get(self.key("open-size", address))
            pipe.zcard(self.key("address", address))
            pipe.zrangebyscore(self.key("open", address), "-inf", before)
            indexed, size, total, ids = pipe.execute()
        if not _index_current(indexed, size, total):
            self._index_open(address)
            ids = self.redis.zrangebyscore(self.key("open", address), "-inf", before)
        return [self.get(item_id) for item_id in ids]

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

    def histories(self, item_ids: list[str]) -> dict[str, list[dict]]:
        if not item_ids:
            return {}
        with self.redis.pipeline(transaction=False) as pipe:
            for item_id in item_ids:
                pipe.lrange(self.key("history", item_id), 0, -1)
            rows = pipe.execute()
        return {item_id: [json.loads(entry) for entry in row] for item_id, row in zip(item_ids, rows)}

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
            keys += [key for key in (self.key("entry", item_id) for item_id in ids) if self.redis.exists(key)]
            keys += self.pool.keys_of(address)
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
        dest = self.home_store(address)
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
            pooled = self.pool.holds(pipe, item)
            moved = replace(
                item,
                address=address or item.address,
                state="pending" if address else "cancelled",
                updated_at=now_ms(),
                reason=reason,
            )
            if dest is not self:
                return self._relocate(pipe, item, moved, dest, by, last, pooled)
            pipe.multi()
            pipe.hset(key, mapping=_fields(moved))
            if moved.address != item.address:
                pipe.hdel(key, POOL_HOLDER, POOL_SKIP)
            if pooled:
                self.pool.stage_remove(pipe, item)
            if moved.state in CLOSED or moved.address != item.address:
                pipe.zrem(self.key("open", item.address), item_id)
            pipe.zrem(self.key("delivered"), item_id)
            pipe.zrem(pending, item_id)
            if last:
                pipe.srem(self.key("waiting"), item.address)
            if address:
                self.stage_pending(pipe, item, address)
            pipe.rpush(self.key("history", item_id), _entry(moved.state, by, reason, moved.updated_at))
            pipe.execute()
            return moved

    def home_store(self, address: str) -> "InboxStore":
        """The inbox a send to address writes: this one, or on the controller the hive member's that holds it."""
        from scripts.hive import scope

        if not address or scope.member():
            return self
        home = self.home(address)
        return self if home == self.world else InboxStore(self.redis, persist=self.persist, world=home)

    def _relocate(self, pipe, item, moved, dest, by, last, pooled):
        """Move item out of this inbox into dest's, pending at moved.address with its history."""
        history, entry = self.key("history", item.id), self.key("entry", item.id)
        pipe.watch(history, entry)
        entries, about = pipe.lrange(history, 0, -1), pipe.get(entry)
        moved = replace(moved, sequence=pipe.incr(dest.key("sequence", moved.address)))
        pipe.multi()
        if pooled:
            self.pool.stage_remove(pipe, item)
        pipe.delete(self.key("item", item.id), history, entry)
        for index in ("address", "open", "pending"):
            pipe.zrem(self.key(index, item.address), item.id)
        pipe.zrem(self.key("delivered"), item.id)
        pipe.decr(self.key("open-size", item.address))
        if last:
            pipe.srem(self.key("waiting"), item.address)
        pipe.hset(dest.key("item", item.id), mapping=_fields(moved))
        pipe.rpush(dest.key("history", item.id), *entries, _entry(moved.state, by, moved.reason, moved.updated_at))
        if about:
            pipe.set(dest.key("entry", item.id), about)
        pipe.zadd(dest.key("address", moved.address), {item.id: item.created_at})
        pipe.zadd(dest.key("open", moved.address), {item.id: item.created_at})
        pipe.incr(dest.key("open-size", moved.address))
        pipe.zadd(dest.key("pending", moved.address), {item.id: item.created_at})
        pipe.sadd(dest.key("waiting"), moved.address)
        if dest.persist is not None:
            dest.persist(pipe)
        pipe.publish(NOTIFY, moved.address)
        pipe.execute()
        return moved

    def move_world(self, item_id: str, target: "InboxStore", by: str, reason: str) -> Item | None:
        """Move an open item, its history and ledger entry into target's world, pending at the same address."""
        from redis.exceptions import WatchError

        for _ in range(MOVE_ATTEMPTS):
            try:
                return self._try_move_world(item_id, target, by, reason)
            except WatchError:
                continue
        raise InboxError(f"message {item_id} changed meanwhile; run the command again")

    def _try_move_world(self, item_id, target, by, reason):
        key, history = self.key("item", item_id), self.key("history", item_id)
        with self.redis.pipeline() as pipe:
            pipe.watch(key, history, self.key("entry", item_id))
            item = _item(pipe.hgetall(key), item_id)
            if item.state in CLOSED:
                return None
            entries = pipe.lrange(history, 0, -1)
            entry = pipe.get(self.key("entry", item_id))
            last = self.last_pending(pipe, item)
            moved = replace(item, state="pending", updated_at=now_ms(), reason=reason)
            pipe.multi()
            pipe.delete(key, history, self.key("entry", item_id))
            pipe.zrem(self.key("address", item.address), item_id)
            pipe.zrem(self.key("open", item.address), item_id)
            pipe.decr(self.key("open-size", item.address))
            pipe.zrem(self.key("pending", item.address), item_id)
            pipe.zrem(self.key("delivered"), item_id)
            if last:
                pipe.srem(self.key("waiting"), item.address)
            pipe.hset(target.key("item", item_id), mapping=_fields(moved))
            pipe.rpush(target.key("history", item_id), *entries, _entry("pending", by, reason, moved.updated_at))
            if entry:
                pipe.set(target.key("entry", item_id), entry)
            pipe.zadd(target.key("address", item.address), {item_id: item.created_at})
            pipe.zadd(target.key("open", item.address), {item_id: item.created_at})
            pipe.incr(target.key("open-size", item.address))
            pipe.zadd(target.key("pending", item.address), {item_id: item.created_at})
            pipe.sadd(target.key("waiting"), item.address)
            if target.persist is not None:
                target.persist(pipe)
            pipe.publish(NOTIFY, item.address)
            pipe.execute()
            return moved

    def _move_address(self, pipe, item, address):
        """pipe is in MULTI: move item's address and open entries, and the open counts, to address."""
        pipe.zrem(self.key("address", item.address), item.id)
        pipe.zadd(self.key("address", address), {item.id: item.created_at})
        pipe.zadd(self.key("open", address), {item.id: item.created_at})
        if address != item.address:
            pipe.zrem(self.key("open", item.address), item.id)
            pipe.decr(self.key("open-size", item.address))
            pipe.incr(self.key("open-size", address))

    def stage_pending(self, pipe, item, address):
        """pipe is in MULTI: index item pending at address, moving its address entry and open count there."""
        self._move_address(pipe, item, address)
        pipe.zadd(self.key("pending", address), {item.id: item.created_at})
        pipe.sadd(self.key("waiting"), address)
        pipe.publish(NOTIFY, address)

    def stage_claim(self, pipe, item, address, by):
        """pipe is in MULTI: hand a pool item to the master seat at address, pending there, its holder recorded."""
        reason = f"claimed from the master pool by {address}"
        moved = replace(item, address=address, updated_at=now_ms(), reason=reason)
        key = self.key("item", item.id)
        pipe.hset(key, mapping={**_fields(moved), POOL_HOLDER: address})
        pipe.hdel(key, POOL_SKIP)
        self.stage_pending(pipe, item, address)
        pipe.rpush(self.key("history", item.id), _entry("pending", by, reason, moved.updated_at))
        return moved

    def _stage_pool_return(self, pipe, item, moved, holder):
        """pipe is in MULTI: unsettled work waits in the pool again, passing its previous holder over."""
        front = master_of(item.address)
        back = replace(moved, address=front)
        key = self.key("item", item.id)
        pipe.hset(key, mapping={**_fields(back), POOL_SKIP: holder})
        pipe.hdel(key, POOL_HOLDER)
        pipe.zrem(self.key("pending", item.address), item.id)
        self._move_address(pipe, item, front)
        self.pool.stage_add(pipe, back)
        return back

    def return_to_pool(self, item_id: str, holder: str, expected_receiver: str = "") -> Item | None:
        """Return unsettled pool work still held by this seat and, when given, the departing receiver."""
        from redis.exceptions import WatchError

        key = self.key("item", item_id)
        with self.redis.pipeline() as pipe:
            try:
                pipe.watch(key)
                raw = pipe.hgetall(key)
                if not raw or raw["state"] in CLOSED or (raw["address"], raw.get(POOL_HOLDER)) != (holder, holder):
                    return None
                if raw["state"] != "pending" and not expected_receiver and not self.pool.reclaimable(pipe, holder):
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
                item = _item(raw, item_id)
                moved = replace(item, state="pending", delivery_session="", updated_at=now_ms(), reason=RECLAIMED)
                pipe.multi()
                moved = self._stage_pool_return(pipe, item, moved, holder)
                pipe.zrem(self.key("delivered"), item_id)
                pipe.rpush(self.key("history", item_id), _entry("pending", REDELIVERER, RECLAIMED, moved.updated_at))
                pipe.execute()
                return moved
            except WatchError:
                return None

    def reply(self, item_id: str, replier: str, text: str, fyi: bool = False, answer_id: str = "") -> Item:
        from redis.exceptions import WatchError

        item = self.get(item_id)
        replier = self.names.resolve(replier)
        if not self.acts_for(replier, item.address):
            raise InboxError(f"message {item_id} belongs to {item.address}, not {replier}")
        if not text.strip():
            raise InboxError("a reply needs text")
        key = self.key("item", item_id)
        field = f"reply:{replier}"
        legacy = answer_id and (item.state, item.reason) == close_reason("done", replied(answer_id))
        if item.state in CLOSED and not self.redis.hexists(key, field) and not legacy:
            raise InboxError(f"message {item_id} is closed: {item.reason}")
        self.redis.hsetnx(key, field, answer_id or new_item_id())
        reply_id = self.redis.hget(key, field)
        if answer_id == reply_id and self.redis.hget(self.key("item", reply_id), "state") == "cancelled":
            raise InboxError(f"answer {reply_id} to message {item_id} was refused")
        if entry := self.entry_of(item_id):
            self.about(reply_id, entry)
        try:
            answer = self.send(replier, item.sender, text, ref=item_id, fyi=fyi, item_id=reply_id)
        except (DuplicateItem, WatchError):
            answer = self.get(reply_id)
            legacy_recipient = not answer.ref and (
                legacy or self.names.resolve(answer.address) == self.names.resolve(item.sender)
            )
            if (self.names.resolve(answer.sender), answer.text, answer.fyi) != (replier, text, fyi) or not (
                answer.ref == item_id or legacy_recipient
            ):
                raise InboxError("a reply retry must keep its sender, recipient, text and informational flag") from None
        self._close_reply(item_id, replier, answer.id, answer_id)
        return answer

    def _close_reply(self, item_id, replier, reply_id, operation_id):
        item = self.get(item_id)
        expected = close_reason("done", replied(reply_id))
        if (item.state, item.reason) == expected:
            if operation_id == reply_id:
                raise DuplicateItem(f"message {item_id} was already answered with message {reply_id}")
            return
        try:
            self.close(item_id, replier, "done", replied(reply_id))
        except InboxError:
            item = self.get(item_id)
            if (item.state, item.reason) != expected:
                raise

    def reopen(self, answer, by, reason):
        """Open the item a refused answer closed, with its answer slot cleared so the next reply is a new message."""
        from redis.exceptions import WatchError

        for _ in range(MOVE_ATTEMPTS):
            try:
                return self._try_reopen(answer, by, reason)
            except WatchError:
                continue
        raise Contended(f"message {answer.ref} changed meanwhile; run the command again")

    def _try_reopen(self, answer, by, reason):
        key = self.key("item", answer.ref)
        with self.redis.pipeline() as pipe:
            pipe.watch(key)
            raw = pipe.hgetall(key)
            if not raw:
                return None
            item = _item(raw, answer.ref)
            if (item.state, item.reason) != close_reason("done", replied(answer.id)):
                return None
            moved = replace(item, state="read", updated_at=now_ms(), reason=reason)
            pipe.multi()
            pipe.hset(key, mapping=_fields(moved))
            pipe.hdel(key, f"reply:{answer.sender}")
            pipe.zadd(self.key("open", item.address), {item.id: item.created_at})
            pipe.rpush(self.key("history", item.id), _entry("read", by, reason, moved.updated_at))
            pipe.publish(NOTIFY, item.address)
            pipe.execute()
            return moved

    def about(self, item_id, entry):
        """Name the ledger entry an item is about, before the item is written."""
        self.redis.set(self.key("entry", item_id), entry)

    def entry_of(self, item_id):
        return self.redis.get(self.key("entry", item_id)) or ""

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
        if not self.session_id:
            return confirmed
        for item_id in self.redis.zrangebyscore(self.key("delivered"), "-inf", before - 1):
            if self.names.resolve(self._delivered_to(item_id)) != receiver:
                continue
            try:
                if self.get(item_id).delivery_session != self.session_id:
                    self.requeue(item_id, receiver, "receiving session changed", before=before - 1)
                    continue
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
        moved = [item for item_id in stale if (item := self.requeue(item_id, REDELIVERER, REDELIVERED, cutoff))]
        self.pool.distribute_all()
        return moved

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
            pooled = self.pool.takes_back(raw.get(POOL_HOLDER))
            pipe.multi()
            pipe.zrem(self.key("delivered"), item_id)
            if not raw or raw["state"] != "delivered":
                pipe.execute()
                return None
            item = _item(raw, item_id)
            moved = replace(item, state="pending", updated_at=now_ms(), reason=reason)
            if pooled:
                moved = self._stage_pool_return(pipe, item, moved, raw[POOL_HOLDER])
            else:
                pipe.hset(key, mapping=_fields(moved))
                pipe.zadd(self.key("pending", item.address), {item_id: item.created_at})
                pipe.sadd(self.key("waiting"), item.address)
                pipe.publish(NOTIFY, item.address)
            pipe.rpush(self.key("history", item_id), _entry("pending", by, reason, moved.updated_at))
            pipe.execute()
            return moved

    def pending(self):
        if not self.redis.exists(self.key("waiting", "built")):
            self._build_waiting()
        addresses = sorted(self.redis.smembers(self.key("waiting")))
        if not addresses:
            return []
        with self.redis.pipeline(transaction=False) as pipe:
            for address in addresses:
                pipe.sismember(self.key("indexed"), address)
                pipe.zrange(self.key("pending", address), 0, -1)
            rows = pipe.execute()
        item_ids = dict.fromkeys(
            item_id
            for address, (indexed, pending) in zip(addresses, zip(*[iter(rows)] * 2))
            for item_id in (pending if indexed else self._back_fill(address))
        )
        return sorted(self._items(list(item_ids)), key=_order)

    def pending_items(self, address):
        return sorted(self._items(self._pending_ids(address)), key=_order)

    def _items(self, item_ids):
        if not item_ids:
            return []
        with self.redis.pipeline(transaction=False) as pipe:
            for item_id in item_ids:
                pipe.hgetall(self.key("item", item_id))
            rows = pipe.execute()
        return [_item(raw, item_id) for item_id, raw in zip(item_ids, rows)]

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
            pooled = self.pool.waiting(pipe, address)
            pending = {item.id: item.created_at for item in items if item.state == "pending" and item.id not in pooled}
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
        raise Contended(f"message {item_id} changed meanwhile; run the command again")

    def _try_move(self, item_id, by, actors, state, reason, only_from):
        key = self.key("item", item_id)
        with self.redis.pipeline() as pipe:
            pipe.watch(key)
            item = _item(pipe.hgetall(key), item_id)
            if not any(self.acts_for(by, address, pipe) for address in actors(item)):
                raise InboxError(f"message {item_id} belongs to {item.address}, not {by}")
            if only_from is not None and item.state not in only_from:
                return None
            if state == "confirmed" and item.state == "delivered" and item.delivery_session != self.session_id:
                return None
            if item.state in CLOSED:
                raise InboxError(f"message {item_id} is closed: {item.reason}")
            if item.fyi and state in ("read", "confirmed"):
                state, reason = "done", "done: information received"
            if state == "done" and not item.fyi and reason == "done":
                raise InboxError("done needs an outcome naming where the work went")
            if item.state == state:
                return item
            if state not in CLOSED and self.pool.holds(pipe, item):
                raise InboxError(f"message {item_id} waits in the master pool for the next master")
            if state == "delivered" and (self._owned(pipe, by) or self._reserved(pipe, item_id)):
                return None
            moved = replace(item, state=state, updated_at=now_ms(), reason=reason)
            if state == "delivered":
                moved = replace(moved, delivery_session=self.session_id)
            last = self.last_pending(pipe, item)
            pipe.multi()
            self.stage_move(pipe, item, moved, by, last)
            pipe.execute()
            return moved

    def _owned(self, pipe, by):
        key = owner_key(NameRegistry(pipe).resolve(by), self.world)
        pipe.watch(key)
        return pipe.get(key) is not None

    def _reserved(self, pipe, item_id):
        """True once a delivery owner may have submitted the item; one only reserved stays deliverable."""
        key = self.key("reservation", item_id)
        pipe.watch(key)
        held = pipe.get(key)
        if not held:
            return False
        pipe.watch(self.key("delivery", held))
        return pipe.hget(self.key("delivery", held), "state") != "reserved"

    def last_pending(self, pipe, item):
        pending = self.key("pending", item.address)
        pipe.watch(pending)
        return pipe.zscore(pending, item.id) is not None and pipe.zcard(pending) == 1

    def stage_move(self, pipe, item, moved, by, last, indexed=True):
        """pipe is already in MULTI; indexed=False keeps a delivered item out of the redelivery index."""
        pipe.hset(self.key("item", item.id), mapping=_fields(moved))
        if moved.state in CLOSED:
            pipe.zrem(self.key("open", item.address), item.id)
            self.pool.stage_remove(pipe, item)
        if moved.state == "delivered" and indexed:
            pipe.zadd(self.key("delivered"), {item.id: moved.updated_at})
        else:
            pipe.zrem(self.key("delivered"), item.id)
        pipe.zrem(self.key("pending", item.address), item.id)
        if last:
            pipe.srem(self.key("waiting"), item.address)
        pipe.rpush(
            self.key("history", item.id),
            _entry(moved.state, by, moved.reason, moved.updated_at, moved.delivery_session),
        )


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


def _entry(state, by, reason, at, session_id=""):
    entry = {"state": state, "by": by, "reason": reason, "at": at}
    if state == "delivered":
        entry["session_id"] = session_id
    return json.dumps(entry)


def connect(environ=None):
    import redis

    from scripts.inbox import outbox
    from scripts.inbox.seen import receiving_session
    from scripts.swarm.store import redis_client

    try:
        store = InboxStore(redis_client(environ), session_id=receiving_session(environ))
    except redis.RedisError as exc:
        raise Unreachable(f"Redis is unreachable ({exc}); the inbox refuses to run without it") from exc
    outbox.flush(store)
    return store
