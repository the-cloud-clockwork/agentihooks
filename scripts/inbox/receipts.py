"""The journal of a recipient's reserved deliveries: submission, acceptance on evidence, commit and recovery."""

from dataclasses import asdict, dataclass, fields, replace

from scripts.inbox.seen import TTL_S, SeenMarks
from scripts.inbox.store import MOVE_ATTEMPTS, InboxError, now_ms, owner_key

OPEN = ("reserved", "submitting", "unknown")
RELEASED = "released on recovery before submission"
REASSIGNED = "the recipient no longer acts for the item address"
MISMATCH = "the accepted payload does not match the reserved one"


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


def transact(redis, body, keys=()):
    """body watches what it reads and calls multi before it stages writes; a concurrent change reruns it."""
    from redis.exceptions import WatchError

    for _ in range(MOVE_ATTEMPTS):
        with redis.pipeline() as pipe:
            try:
                if keys:
                    pipe.watch(*keys)
                result = body(pipe)
                pipe.execute()
                return result
            except WatchError:
                continue
    raise DispatchError("the inbox changed meanwhile; run it again")


def check_owner(pipe, recipient, owner):
    pipe.watch(owner_key(recipient))
    held = pipe.get(owner_key(recipient))
    if held != owner:
        raise DispatchError(f"{owner} does not deliver for {recipient}; {held or 'nobody'} does")


class Receipts:
    def __init__(self, store):
        self.store = store
        self.redis = store.redis

    def key(self, *parts):
        return self.store.key(*parts)

    def get(self, delivery_id):
        return _delivery(self.redis.hgetall(self.key("delivery", delivery_id)), delivery_id)

    def submitting(self, delivery_id, owner):
        return transact(self.redis, lambda pipe: self._advance(pipe, delivery_id, owner, ("reserved",), "submitting"))

    def accept(self, delivery_id, owner, evidence):
        if evidence != self.get(delivery_id).digest:
            return self.reject(delivery_id, owner, MISMATCH)
        transact(
            self.redis, lambda pipe: self._advance(pipe, delivery_id, owner, ("submitting", "unknown"), "accepted")
        )
        return self._commit(delivery_id, owner)

    def reject(self, delivery_id, owner, reason):
        def close(pipe):
            delivery = self._open(pipe, delivery_id, owner, OPEN)
            pipe.multi()
            return self._stage_close(pipe, delivery, "rejected", reason)

        return transact(self.redis, close)

    def recover(self, recipient, owner):
        recipient = self.store.names.resolve(recipient)
        held = self.redis.get(owner_key(recipient))
        if held != owner:
            raise DispatchError(f"{owner} does not deliver for {recipient}; {held or 'nobody'} does")
        recovered = []
        for delivery_id in sorted(self.redis.smembers(self.key("deliveries", recipient))):
            state = self.get(delivery_id).state
            if state == "reserved":
                recovered.append(self.reject(delivery_id, owner, RELEASED))
            elif state == "submitting":
                recovered.append(
                    transact(self.redis, lambda pipe: self._advance(pipe, delivery_id, owner, (state,), "unknown"))
                )
            elif state == "accepted":
                recovered.append(self._commit(delivery_id, owner))
            else:
                recovered.append(self.get(delivery_id))
        return recovered

    def _commit(self, delivery_id, owner):
        return transact(self.redis, lambda pipe: self._try_commit(pipe, delivery_id, owner))

    def _try_commit(self, pipe, delivery_id, owner):
        delivery = self._open(pipe, delivery_id, owner, ("accepted",))
        pipe.watch(self.key("item", delivery.item))
        item = self.store.get(delivery.item)
        if item.state != "pending" or not self.store.acts_for(delivery.recipient, item.address, pipe):
            pipe.multi()
            return self._stage_close(pipe, delivery, "superseded", REASSIGNED)
        last = self.store.last_pending(pipe, item)
        pipe.multi()
        delivered = replace(item, state="delivered", updated_at=now_ms(), reason="")
        self.store.stage_move(pipe, item, delivered, delivery.recipient, last, indexed=False)
        if delivery.ref:
            pipe.sadd(SeenMarks.key(delivery.recipient), delivery.ref)
            pipe.expire(SeenMarks.key(delivery.recipient), TTL_S)
        return self._stage_close(pipe, delivery, "accepted", "", committed=True)

    def _advance(self, pipe, delivery_id, owner, before, state):
        delivery = self._open(pipe, delivery_id, owner, before)
        moved = replace(delivery, state=state, owner=owner, at=now_ms())
        pipe.multi()
        pipe.hset(self.key("delivery", delivery_id), mapping=delivery_fields(moved))
        return moved

    def _open(self, pipe, delivery_id, owner, before):
        key = self.key("delivery", delivery_id)
        pipe.watch(key)
        delivery = _delivery(pipe.hgetall(key), delivery_id)
        check_owner(pipe, delivery.recipient, owner)
        if delivery.state not in before or delivery.committed:
            raise DispatchError(f"delivery {delivery_id} is {delivery.state}")
        return delivery

    def _stage_close(self, pipe, delivery, state, reason, committed=False):
        closed = replace(delivery, state=state, reason=reason, at=now_ms(), committed=committed)
        pipe.hset(self.key("delivery", delivery.id), mapping=delivery_fields(closed))
        pipe.delete(self.key("reservation", delivery.item))
        if delivery.ref:
            pipe.delete(self.key("ref-reservation", delivery.recipient, delivery.ref))
        pipe.srem(self.key("deliveries", delivery.recipient), delivery.id)
        return closed


def delivery_fields(delivery):
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
