from collections.abc import Callable
from typing import Any

from scripts.events import nats_payload


def answer_unchecked(loop: Any, msg: Any, handler: Callable[[str, bytes], bytes]) -> None:
    reply = handler(msg.subject, msg.data)
    # ruleid: nats-reply-without-size-path
    loop.run(msg.respond(reply))


def answer_validated(loop: Any, client: Any, msg: Any, handler: Callable[[str, bytes], bytes]) -> None:
    reply = handler(msg.subject, msg.data)
    nats_payload.validate(client, msg.reply, reply)
    # ruleid: nats-reply-without-size-path
    loop.run(msg.respond(reply))


def answer_fitted(loop: Any, client: Any, msg: Any, handler: Callable[[str, bytes], bytes]) -> None:
    # ok: nats-reply-without-size-path
    loop.run(msg.respond(nats_payload.fitted(client, msg.reply, handler(msg.subject, msg.data))))


def answer_fitted_first(loop: Any, client: Any, msg: Any, handler: Callable[[str, bytes], bytes]) -> None:
    sent = nats_payload.fitted(client, msg.reply, handler(msg.subject, msg.data))
    # ok: nats-reply-without-size-path
    loop.run(msg.respond(sent))
