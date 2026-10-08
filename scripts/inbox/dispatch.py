"""Delivery reservations for an opted in recipient: one owner reserves pending items and commits them on accepted evidence.

Legacy recipients keep claim(); while an owner holds a recipient, claim and the seen marks refuse it.
"""

import hashlib
import json
import uuid
from dataclasses import replace

from scripts.inbox.receipts import Delivery, DispatchError, Receipts, check_owner, delivery_fields, transact
from scripts.inbox.seen import SEEN_ON_LEDGER, SeenMarks
from scripts.inbox.store import close_reason, now_ms, owner_key

SHOWN = "its ref was already accepted or shown"


def digest(item) -> str:
    payload = {name: getattr(item, name) for name in ("id", "sender", "address", "text", "ref", "task")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class Dispatcher:
    def __init__(self, store):
        self.store = store
        self.redis = store.redis
        self.marks = SeenMarks(store.redis)
        self.receipts = Receipts(store)

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

        transact(self.redis, claim, [owner_key(recipient)])

    def release(self, recipient, owner):
        recipient = self.store.names.resolve(recipient)

        def drop(pipe):
            held = pipe.get(owner_key(recipient))
            if held == owner and pipe.scard(self.key("deliveries", recipient)):
                raise DispatchError(f"{recipient} still has open deliveries; accept or reject them before releasing")
            pipe.multi()
            if held != owner:
                return False
            pipe.delete(owner_key(recipient))
            return True

        return transact(self.redis, drop, [owner_key(recipient), self.key("deliveries", recipient)])

    def reserve(self, recipient, owner):
        """Reserve each pending item in inbox order; an item whose ref was accepted or shown closes as shown."""
        recipient = self.store.names.resolve(recipient)
        items = self.store.pending_mail(recipient)
        return transact(self.redis, lambda pipe: self._reserve(pipe, recipient, owner, items))

    def _reserve(self, pipe, recipient, owner, items):
        check_owner(pipe, recipient, owner)
        plan = self._plan(pipe, recipient, items)
        pipe.multi()
        reserved = []
        for item, state, last in plan:
            delivery = Delivery(
                uuid.uuid4().hex[:12], recipient, owner, item.id, item.ref, digest(item), "reserved", now_ms()
            )
            if state == "shown":
                superseded = replace(delivery, state="superseded", reason=SHOWN)
                pipe.hset(self.key("delivery", delivery.id), mapping=delivery_fields(superseded))
                reason = close_reason("done", SEEN_ON_LEDGER)[1]
                self.store.stage_move(
                    pipe, item, replace(item, state="done", updated_at=now_ms(), reason=reason), recipient, last
                )
                continue
            pipe.hset(self.key("delivery", delivery.id), mapping=delivery_fields(delivery))
            pipe.set(self.key("reservation", item.id), delivery.id)
            if item.ref:
                pipe.set(self.key("ref-reservation", recipient, item.ref), delivery.id)
            pipe.sadd(self.key("deliveries", recipient), delivery.id)
            reserved.append(delivery)
        return reserved

    def _plan(self, pipe, recipient, items):
        """(item, free or shown, last pending) for each item still pending for recipient and not reserved."""
        plan, taken = [], set()
        for listed in items:
            pipe.watch(self.key("item", listed.id), self.key("reservation", listed.id))
            item = self.store.get(listed.id)
            if item.state != "pending" or pipe.exists(self.key("reservation", item.id)):
                continue
            if not self.store.acts_for(recipient, item.address, pipe):
                continue
            state = self._ref_state(pipe, recipient, item.ref, taken)
            if state == "held":
                continue
            if state == "free":
                taken.add(item.ref)
            plan.append((item, state, state == "shown" and self.store.last_pending(pipe, item)))
        return plan

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
