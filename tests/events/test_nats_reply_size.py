"""A bus reply crosses the server payload limit as it is, compressed, or as its serve's refusal, never silently."""

import asyncio
import json
import logging
import random
import zlib
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from scripts.events import nats, nats_payload
from scripts.events.bus import PayloadTooLarge

pytestmark = pytest.mark.unit


def served(limit: int, reply: bytes, **options: Any) -> tuple[Any, SimpleNamespace]:
    """A bus under a server limit that answered one request with the reply, and the message it answered."""
    opened: Any = object.__new__(nats.NatsBus)
    opened._loop = SimpleNamespace(run=asyncio.run)
    opened._serving = ThreadPoolExecutor(1)
    opened._serve_timing = nats.ServeTiming(nats.SERVE_WORKERS)
    opened._nc = SimpleNamespace(max_payload=limit, subscribe=AsyncMock(), flush=AsyncMock(), request=AsyncMock())
    opened.serve("service", "workers", lambda subject, data: reply, **options)
    message = SimpleNamespace(subject="service", data=b"ask", reply="reply.subject", respond=AsyncMock())
    asyncio.run(opened._nc.subscribe.await_args.kwargs["cb"](message))
    opened._serving.shutdown(wait=True)
    return opened, message


def read(opened: Any, sent: bytes) -> bytes:
    """What a requester on the bus reads when the server delivers the sent reply."""
    opened._nc.request = AsyncMock(return_value=SimpleNamespace(data=sent))
    return opened.request("service", b"ask", 100)


@pytest.mark.feature("hub-admission-reply-fits-bus")
def test_a_reply_at_the_server_limit_is_answered_and_read_as_it_is() -> None:
    reply = json.dumps({"ok": True, "result": "x" * 80}).encode()
    opened, message = served(len(reply), reply)

    message.respond.assert_awaited_once_with(reply)
    assert read(opened, reply) == reply


@pytest.mark.feature("hub-admission-reply-fits-bus")
def test_a_reply_over_the_server_limit_is_answered_compressed_and_read_back_whole() -> None:
    reply = json.dumps({"ok": True, "result": {"recaps": ["the same recap text"] * 400}}).encode()
    compact = nats_payload.COMPRESSED + zlib.compress(reply)
    assert len(compact) < len(reply)
    opened, message = served(len(compact), reply)

    message.respond.assert_awaited_once_with(compact)
    assert read(opened, compact) == reply


@pytest.mark.feature("hub-admission-reply-fits-bus")
def test_a_reply_that_does_not_fit_compressed_is_answered_by_the_refusal() -> None:
    refusals: list[tuple[str, bytes, str]] = []

    def refusal(subject: str, data: bytes, error: PayloadTooLarge) -> bytes:
        refusals.append((subject, data, str(error)))
        return b"refused"

    _, message = served(100, random.Random(7).randbytes(400), refusal=refusal)

    assert refusals == [("service", b"ask", "reply.subject: payload 400 exceeds server limit 100")]
    message.respond.assert_awaited_once_with(b"refused")


@pytest.mark.feature("hub-admission-reply-fits-bus")
def test_a_refusal_answer_over_the_server_limit_is_logged_and_never_sent(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.ERROR, logger=nats.__name__)
    _, message = served(100, random.Random(7).randbytes(400), refusal=lambda subject, data, error: b"x" * 101)

    assert [record.getMessage() for record in caplog.records if record.name == nats.__name__] == [
        "reply.subject: payload 101 exceeds server limit 100"
    ]
    message.respond.assert_not_awaited()
