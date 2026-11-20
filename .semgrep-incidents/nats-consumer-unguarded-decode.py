from typing import Any

from nats.aio.msg import Msg

from scripts.events.envelope import Envelope
from scripts.events.nats import NatsDelivery, _undecodable


class Unsafe:
    def __init__(self, message: Msg, loop: Any) -> None:
        self.message = message
        self.loop = loop

    def _fetch(self) -> Envelope:
        # ruleid: nats-consumer-unguarded-decode
        return Envelope.decode(self.message.data)

    def fetch(self) -> NatsDelivery:
        # ruleid: nats-consumer-unguarded-decode
        return NatsDelivery(self.loop, self.message)


class Safe:
    def __init__(self, message: Msg) -> None:
        self.message = message

    def _fetch(self) -> Envelope:
        try:
            # ok: nats-consumer-unguarded-decode
            return Envelope.decode(self.message.data)
        except ValueError:
            return _undecodable(self.message)
