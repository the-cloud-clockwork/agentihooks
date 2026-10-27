import functools
import hashlib
import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace

from scripts.events.bus import Bus, Delivery, StateConflict, StateStore
from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.events.factory import open_bus
from scripts.events.subjects import subject
from scripts.inbox.seats import is_seat
from scripts.inbox.store import CLOSED, InboxError

FEED_BACKEND = "nats"
FEED_BATCH = 10
REOPEN_S = 30.0


def _identity(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def _encode(value: dict) -> bytes:
    return json.dumps(value).encode()


@dataclass(frozen=True)
class Message:
    id: str
    sender: str
    text: str
    ref: str = ""
    fyi: bool = False
    task: str = ""


@dataclass(frozen=True)
class Receipt:
    event: Envelope
    session: str
    revision: int
    delivery: Delivery


class SentElsewhere(ValueError):
    pass


class SeatInbox:
    def __init__(self, bus: Bus, swarm: str):
        if not swarm:
            raise ValueError("a seat inbox needs a swarm")
        self.bus = bus
        self.swarm = swarm
        self.state = bus.state("inbox")

    def send(self, address: str, message: Message) -> Envelope:
        if not address or not message.text.strip():
            raise ValueError("a message needs an address and text")
        event = Envelope(
            id=message.id,
            type="inbox.sent",
            swarm=self.swarm,
            entity="inbox",
            key=address,
            version=1,
            time=time.time_ns() // 1_000_000,
            author=message.sender,
            data=asdict(message),
        )
        key = f"send.{_identity(message.id)}"
        try:
            self.state.create(key, event.encode())
        except StateConflict:
            event = Envelope.decode(self.state.get(key).value)
            if (event.swarm, event.key, event.data) != (self.swarm, address, asdict(message)):
                raise SentElsewhere("item id already belongs to a different message") from None
        self.bus.events.publish(_subject(self.swarm, address), event)
        publish_wake(self.bus, event)
        return event

    def consumer(self, address: str, session: str) -> "SeatConsumer":
        if not address or not session:
            raise ValueError("a consumer needs a seat address and receiving session")
        return SeatConsumer(self.bus, self.state, self.swarm, address, session)


def publish_wake(bus: Bus, event: Envelope) -> None:
    request = replace(
        event,
        id=f"wake-{event.id}",
        type="inbox.wake",
        entity="inbox-wake",
        key=event.data["id"],
        data={"address": event.key},
    )
    bus.events.publish(subject(event.swarm, "inbox-wake", "requested"), request)


def _subject(swarm: str, address: str) -> str:
    return subject(swarm, "inbox", _identity(address))


class SeatConsumer:
    def __init__(self, bus: Bus, state: StateStore, swarm: str, address: str, session: str):
        self._state = state
        self._swarm = swarm
        self._address = address
        self._session = session
        self.durable = f"inbox-{_identity(swarm, address)}"
        self.subject = _subject(swarm, address)
        self._consumer = bus.events.consumer(self.durable, self.subject)

    def fetch(self, batch: int, timeout_ms: int) -> list[Receipt]:
        receipts = []
        for delivery in self._consumer.fetch(batch, timeout_ms):
            receipt = self._claim(delivery)
            if receipt is not None:
                receipts.append(receipt)
        return receipts

    def _key(self, item_id: str) -> str:
        return f"delivery.{_identity(self._swarm, self._address, item_id)}"

    def _claim(self, delivery: Delivery) -> Receipt | None:
        key = self._key(delivery.event.id)
        entry = self._state.get(key)
        if entry is not None:
            previous = json.loads(entry.value)
            if previous["confirmed"]:
                delivery.ack()
                return None
            if previous["attempt"] >= delivery.attempt:
                return None
        value = _encode({"confirmed": False, "session": self._session, "attempt": delivery.attempt})
        try:
            revision = (
                self._state.create(key, value) if entry is None else self._state.update(key, value, entry.revision)
            )
        except StateConflict:
            return None
        return Receipt(delivery.event, self._session, revision, delivery)

    def confirm(self, receipt: Receipt) -> bool:
        if (receipt.session, receipt.event.swarm, receipt.event.key) != (
            self._session,
            self._swarm,
            self._address,
        ):
            return False
        key = self._key(receipt.event.id)
        entry = self._state.get(key)
        if entry is None:
            return False
        record = json.loads(entry.value)
        if record["session"] != self._session:
            return False
        if record["confirmed"]:
            if record["claim"] != receipt.revision:
                return False
        else:
            if entry.revision != receipt.revision:
                return False
            record.update(confirmed=True, claim=receipt.revision)
            try:
                self._state.update(key, _encode(record), entry.revision)
            except StateConflict:
                return False
        receipt.delivery.ack()
        return True


class FeedError(RuntimeError):
    pass


def _swarm_of(seat: str) -> str:
    return seat.partition("@")[2]


def _nats() -> BusConfig | None:
    config = BusConfig.from_environ(os.environ)
    return config if config.backend == FEED_BACKEND else None


def _drop(bus: Bus | None) -> None:
    if bus is not None:
        try:
            bus.close()
        except Exception:
            pass


def seat_of(store, me: str) -> str:
    me = store.names.resolve(me)
    seat = store.seats.seat_of(me)
    return seat if seat and not store.seats.left(me, seat) else ""


class SeatPublisher:
    def __init__(self, opener: Callable[[], Bus]):
        self._opener = opener
        self._bus = None
        self._retry_at = 0.0

    def publish(self, item, swarm="") -> None:
        swarm = _swarm_of(item.address) if is_seat(item.address) else swarm
        if not swarm or (self._bus is None and time.monotonic() < self._retry_at):
            return
        try:
            if self._bus is None:
                self._bus = self._opener()
            message = Message(item.id, item.sender, item.text, item.ref, item.fyi, item.task)
            if is_seat(item.address):
                SeatInbox(self._bus, swarm).send(item.address, message)
            else:
                event = Envelope(
                    id=item.id,
                    type="inbox.sent",
                    swarm=swarm,
                    entity="inbox",
                    key=item.address,
                    version=1,
                    time=item.created_at,
                    author=item.sender,
                    data=asdict(message),
                )
                publish_wake(self._bus, event)
        except Exception as exc:
            _drop(self._bus)
            self._bus, self._retry_at = None, time.monotonic() + REOPEN_S
            print(
                f"agentihooks inbox: bus publish of {item.id} failed ({exc}); the Redis recheck delivers it",
                file=sys.stderr,
            )


@functools.cache
def _publisher(config: BusConfig) -> SeatPublisher:
    return SeatPublisher(lambda: open_bus(config))


def publish(item, names=None) -> None:
    config = _nats()
    if config is not None:
        swarm = names.slug_of(item.address) if names is not None and not is_seat(item.address) else ""
        _publisher(config).publish(item, swarm)


class SeatFeed:
    """Keep durable receipts until Redis records session confirmation or explicit settlement."""

    def __init__(self, opener: Callable[[], Bus], store, me: str, session: str, seat: Callable[[], str] | None = None):
        self._opener = opener
        self._store = store
        self._seat = seat or (lambda: seat_of(store, me))
        self._session = session
        self._bus = None
        self._consumers: dict[str, SeatConsumer] = {}
        self._retry_at = 0.0
        self.held: dict[str, Receipt] = {}

    def _bind(self) -> SeatConsumer | None:
        if self._bus is None and time.monotonic() < self._retry_at:
            return None
        address = self._seat()
        if not address:
            return None
        consumer = self._consumers.get(address)
        if consumer is None:
            if self._bus is None:
                self._bus = self._opener()
            consumer = SeatInbox(self._bus, _swarm_of(address)).consumer(address, self._session)
            self._consumers[address] = consumer
        return consumer

    def wait(self, timeout_ms: int) -> bool:
        try:
            consumer = self._bind()
            receipts = consumer.fetch(FEED_BATCH, timeout_ms) if consumer is not None else []
        except Exception as exc:
            raise self._lost(exc) from exc
        if consumer is None:
            time.sleep(timeout_ms / 1000)
        for receipt in receipts:
            self.held[receipt.event.id] = receipt
        return bool(receipts)

    def settle(self) -> list[str]:
        settled = []
        try:
            consumer = self._bind()
            for item_id, receipt in list(self.held.items()):
                if self._pending(item_id):
                    continue
                if consumer is not None and consumer.confirm(receipt):
                    del self.held[item_id]
                    settled.append(item_id)
        except Exception as exc:
            raise self._lost(exc) from exc
        return settled

    def _lost(self, exc: Exception) -> FeedError:
        self.close()
        self._retry_at = time.monotonic() + REOPEN_S
        return FeedError(str(exc))

    def _pending(self, item_id: str) -> bool:
        try:
            item = self._store.get(item_id)
        except InboxError:
            return True
        if item.state in CLOSED:
            return False
        return item.state not in ("read", "confirmed")

    def close(self) -> None:
        bus, self._bus = self._bus, None
        self._consumers.clear()
        self.held.clear()
        _drop(bus)


def feed_for(store, me: str, session: str = "") -> SeatFeed | None:
    config = _nats()
    if config is None:
        return None
    return SeatFeed(lambda: open_bus(config), store, me, session or store.session_id or me)
