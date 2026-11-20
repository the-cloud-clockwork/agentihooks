from typing import Any

from nats.aio.msg import Msg

from scripts.events.envelope import Envelope
from scripts.events.nats import NatsDelivery, _undecodable


class Unsafe:
    def __init__(self, message: Msg, loop: Any) -> None:
        self.message = message
        self.loop = loop

    def _fetch(self) -> Envelope:
        try:
            # ruleid: nats-consumer-unguarded-decode
            return Envelope.decode(self.message.data)
        except ValueError:
            return _undecodable(self.message)

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
        except (ValueError, RecursionError):
            return _undecodable(self.message)


class SafeDelivery:
    def __init__(self, message: Msg, loop: Any) -> None:
        self.message = message
        self.loop = loop

    def fetch(self, message: Msg) -> list[NatsDelivery]:
        try:
            # ok: nats-consumer-unguarded-decode
            delivery = NatsDelivery(self.loop, message)
        except (ValueError, RecursionError):
            return []
        else:
            return [delivery]
