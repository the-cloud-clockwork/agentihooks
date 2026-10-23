import hashlib
import json
import time
from dataclasses import asdict, dataclass

from scripts.events.bus import Bus, Delivery, StateConflict, StateStore
from scripts.events.envelope import Envelope
from scripts.events.subjects import subject


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
                raise ValueError("item id already belongs to a different message") from None
        self.bus.events.publish(_subject(self.swarm, address), event)
        return event

    def consumer(self, address: str, session: str) -> "SeatConsumer":
        if not address or not session:
            raise ValueError("a consumer needs a seat address and receiving session")
        return SeatConsumer(self.bus, self.state, self.swarm, address, session)


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
