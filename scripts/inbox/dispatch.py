"""Delivery reservations for an opted in recipient: one owner reserves pending items and commits them on accepted evidence.

Legacy recipients keep claim(); while an owner holds a recipient, claim and the seen marks refuse it.
"""

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, fields, replace

from scripts.inbox.seen import SEEN_ON_LEDGER, TTL_S, SeenMarks
from scripts.inbox.store import MOVE_ATTEMPTS, InboxError, now_ms, owner_key

OPEN = ("reserved", "submitting", "unknown")
RELEASED = "released on recovery before submission"
REASSIGNED = "the recipient no longer acts for the item address"
MISMATCH = "the accepted payload does not match the reserved one"
SHOWN = "its ref was already accepted or shown"


class DispatchError(InboxError):
    pass


@dataclass(frozen=True)
class Delivery:
    id: str
    recipient: str
    owner: str
    item: str
    ref: str
    digest: str
    state: str
    at: int
    reason: str = ""
    committed: bool = False


def digest(item) -> str:
    payload = {name: getattr(item, name) for name in ("id", "sender", "address", "text", "ref", "task")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class Dispatcher:
    def __init__(self, store):
        self.store = store
        self.redis = store.redis
        self.marks = SeenMarks(store.redis)

    def key(self, *parts):
        return self.store.key(*parts)

    def owner(self, recipient):
        return self.redis.get(owner_key(self.store.names.resolve(recipient))) or ""

    def own(self, recipient, owner, takeover=False):
        recipient = self.store.names.resolve(recipient)

        def claim(pipe):
            held = pipe.get(owner_key(recipient))
            if held and held != owner and not takeover:
                raise DispatchError(f"{recipient} is delivered by {held}; take over to replace it")
            pipe.multi()
            pipe.set(owner_key(recipient), owner)

        self._transact([owner_key(recipient)], claim)

    def release(self, recipient, owner):
        recipient = self.store.names.resolve(recipient)

        def drop(pipe):
            held = pipe.get(owner_key(recipient))
            pipe.multi()
            if held != owner:
                return False
            pipe.delete(owner_key(recipient))
            return True

        return self._transact([owner_key(recipient)], drop)

    def get(self, delivery_id):
        return _delivery(self.redis.hgetall(self.key("delivery", delivery_id)), delivery_id)

    def reserve(self, recipient, owner):
        """Reserve each pending item in inbox order; an item whose ref was accepted or shown closes as shown."""
        recipient = self.store.names.resolve(recipient)
        items = self.store.pending_mail(recipient)
        reserved, superseded = self._transact([], lambda pipe: self._reserve(pipe, recipient, owner, items))
        for item in superseded:
            self.store.close(item.id, recipient, "done", SEEN_ON_LEDGER)
        return reserved

    def submitting(self, delivery_id, owner):
        return self._transact([], lambda pipe: self._advance(pipe, delivery_id, owner, ("reserved",), "submitting"))

    def accept(self, delivery_id, owner, evidence):
        """Accept a submission whose payload digest matches the reserved one, then commit it."""
        if evidence != self.get(delivery_id).digest:
            return self.reject(delivery_id, owner, MISMATCH)
        self._transact([], lambda pipe: self._advance(pipe, delivery_id, owner, ("submitting", "unknown"), "accepted"))
        return self._commit(delivery_id, owner)

    def reject(self, delivery_id, owner, reason):
        def close(pipe):
            delivery = self._open(pipe, delivery_id, owner, OPEN)
            pipe.multi()
            return self._stage_close(pipe, delivery, "rejected", reason)

        return self._transact([], close)

    def recover(self, recipient, owner):
        """After a crash: release reserved items, mark a submission unknown, commit an accepted one."""
        recipient = self.store.names.resolve(recipient)
        if self.owner(recipient) != owner:
            raise DispatchError(f"{owner} does not deliver for {recipient}; {self.owner(recipient) or 'nobody'} does")
        recovered = []
        for delivery_id in sorted(self.redis.smembers(self.key("deliveries", recipient))):
            state = self.get(delivery_id).state
            if state == "reserved":
                recovered.append(self.reject(delivery_id, owner, RELEASED))
            elif state == "submitting":
                recovered.append(
                    self._transact([], lambda pipe: self._advance(pipe, delivery_id, owner, (state,), "unknown"))
                )
            elif state == "accepted":
                recovered.append(self._commit(delivery_id, owner))
            else:
                recovered.append(self.get(delivery_id))
        return recovered

    def _commit(self, delivery_id, owner):
        return self._transact([], lambda pipe: self._try_commit(pipe, delivery_id, owner))

    def _try_commit(self, pipe, delivery_id, owner):
        delivery = self._open(pipe, delivery_id, owner, ("accepted",))
        pipe.watch(self.key("item", delivery.item))
        item = self.store.get(delivery.item)
        if item.state != "pending" or not self.store.acts_for(delivery.recipient, item.address, pipe):
            pipe.multi()
            return self._stage_close(pipe, delivery, "superseded", REASSIGNED)
        last = self.store.last_pending(pipe, item)
        pipe.multi()
        self.store.stage_move(
            pipe, item, replace(item, state="delivered", updated_at=now_ms(), reason=""), delivery.recipient, last
        )
        if delivery.ref:
            pipe.sadd(self.marks.key(delivery.recipient), delivery.ref)
            pipe.expire(self.marks.key(delivery.recipient), TTL_S)
        return self._stage_close(pipe, delivery, "accepted", "", committed=True)

    def _reserve(self, pipe, recipient, owner, items):
        self._check_owner(pipe, recipient, owner)
        plan, taken = [], set()
        for item in items:
            pipe.watch(self.key("item", item.id), self.key("reservation", item.id))
            if pipe.hget(self.key("item", item.id), "state") != "pending" or pipe.exists(
                self.key("reservation", item.id)
            ):
                continue
            state = self._ref_state(pipe, recipient, item.ref, taken)
            if state != "held":
                plan.append((item, state))
                taken.add(item.ref)
        pipe.multi()
        reserved, superseded = [], []
        for item, state in plan:
            delivery = Delivery(
                uuid.uuid4().hex[:12], recipient, owner, item.id, item.ref, digest(item), "reserved", now_ms()
            )
            if state == "shown":
                pipe.hset(
                    self.key("delivery", delivery.id),
                    mapping=_fields(replace(delivery, state="superseded", reason=SHOWN)),
                )
                superseded.append(item)
                continue
            pipe.hset(self.key("delivery", delivery.id), mapping=_fields(delivery))
            pipe.set(self.key("reservation", item.id), delivery.id)
            if item.ref:
                pipe.set(self.key("ref-reservation", recipient, item.ref), delivery.id)
            pipe.sadd(self.key("deliveries", recipient), delivery.id)
            reserved.append(delivery)
        return reserved, superseded

    def _ref_state(self, pipe, recipient, ref, taken):
        """free, held by an open delivery, or shown: seen, or held by an accepted one."""
        if not ref:
            return "free"
        if ref in taken:
            return "held"
        holder_key = self.key("ref-reservation", recipient, ref)
        pipe.watch(self.marks.key(recipient), holder_key)
        if pipe.sismember(self.marks.key(recipient), ref):
            return "shown"
        holder = pipe.get(holder_key)
        if not holder:
            return "free"
        pipe.watch(self.key("delivery", holder))
        return "shown" if pipe.hget(self.key("delivery", holder), "state") == "accepted" else "held"

    def _advance(self, pipe, delivery_id, owner, before, state):
        delivery = self._open(pipe, delivery_id, owner, before)
        moved = replace(delivery, state=state, owner=owner, at=now_ms())
        pipe.multi()
        pipe.hset(self.key("delivery", delivery_id), mapping=_fields(moved))
        return moved

    def _open(self, pipe, delivery_id, owner, before):
        key = self.key("delivery", delivery_id)
        pipe.watch(key)
        delivery = _delivery(pipe.hgetall(key), delivery_id)
        self._check_owner(pipe, delivery.recipient, owner)
        if delivery.state not in before or delivery.committed:
            raise DispatchError(f"delivery {delivery_id} is {delivery.state}")
        return delivery

    def _check_owner(self, pipe, recipient, owner):
        pipe.watch(owner_key(recipient))
        held = pipe.get(owner_key(recipient))
        if held != owner:
            raise DispatchError(f"{owner} does not deliver for {recipient}; {held or 'nobody'} does")

    def _stage_close(self, pipe, delivery, state, reason, committed=False):
        closed = replace(delivery, state=state, reason=reason, at=now_ms(), committed=committed)
        pipe.hset(self.key("delivery", delivery.id), mapping=_fields(closed))
        pipe.delete(self.key("reservation", delivery.item))
        if delivery.ref:
            pipe.delete(self.key("ref-reservation", delivery.recipient, delivery.ref))
        pipe.srem(self.key("deliveries", delivery.recipient), delivery.id)
        return closed

    def _transact(self, keys, body):
        from redis.exceptions import WatchError

        for _ in range(MOVE_ATTEMPTS):
            with self.redis.pipeline() as pipe:
                try:
                    if keys:
                        pipe.watch(*keys)
                    result = body(pipe)
                    pipe.execute()
                    return result
                except WatchError:
                    continue
        raise DispatchError("the inbox changed meanwhile; run it again")


def _fields(delivery):
    return {name: str(value) for name, value in asdict(delivery).items()}


def _delivery(raw, delivery_id):
    if not raw:
        raise DispatchError(f"no delivery {delivery_id}")
    known = {field.name for field in fields(Delivery)}
    return Delivery(
        **{
            **{name: value for name, value in raw.items() if name in known},
            "at": int(raw["at"]),
            "committed": raw.get("committed") == "True",
        }
    )
