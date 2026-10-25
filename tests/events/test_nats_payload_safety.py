import asyncio
import random
import zlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from nats.js.errors import APIError, NotFoundError

from scripts.events import nats, nats_payload
from scripts.events.nats_state import ScopedStateStore

pytestmark = pytest.mark.unit

SCOPE = nats_payload.scope("key", 9)


def test_codec_keeps_fitting_values_inline_and_rejects_an_unusable_budget():
    write = AsyncMock()
    assert asyncio.run(nats_payload.encode(write, b"a" * 128, 128, SCOPE)) == b"a" * 128
    write.assert_not_awaited()
    with pytest.raises(ValueError, match="^server payload budget is too small for state references$"):
        asyncio.run(nats_payload.encode(write, b"x", 127, SCOPE))
    value = random.Random(3).randbytes(512) * 4
    compressed = nats_payload.COMPRESSED + zlib.compress(value)
    assert asyncio.run(nats_payload.encode(write, value, len(compressed), SCOPE)) == compressed
    write.assert_not_awaited()


def test_page_geometry_remains_readable_across_budgets_without_empty_pages():
    pages = {}
    value = random.Random(2).randbytes(1024) * 4
    compressed = nats_payload.COMPRESSED + zlib.compress(value)
    while len(compressed) % 2:
        value += b"x"
        compressed = nats_payload.COMPRESSED + zlib.compress(value)

    async def write(key, chunk):
        assert chunk
        pages.setdefault(key, chunk)

    async def read(key):
        return pages[key]

    for budget in [128, 256, len(compressed) // 2, len(compressed) - 1]:
        encoded = asyncio.run(nats_payload.encode(write, value, budget, SCOPE))
        assert len(encoded) <= budget
        assert asyncio.run(nats_payload.decode(read, encoded)) == value


def test_publish_preserves_stream_and_counts_its_exact_wire_header():
    ack = object()
    js = SimpleNamespace(_nc=SimpleNamespace(max_payload=100), publish=AsyncMock(return_value=ack))
    assert asyncio.run(nats_payload.publish(js, "subject", b"x" * 58, stream="EVENTS", headers={})) is ack
    js.publish.assert_awaited_once_with("subject", b"x" * 58, stream="EVENTS", headers={})
    with pytest.raises(ValueError, match="subject: payload 101 exceeds server limit 100"):
        asyncio.run(nats_payload.publish(js, "subject", b"x" * 59, stream="EVENTS", headers={}))


def test_requests_and_replies_report_the_refused_subject_before_sending():
    opened = object.__new__(nats.NatsBus)
    opened._loop = SimpleNamespace(run=asyncio.run)
    opened._nc = SimpleNamespace(max_payload=10, subscribe=AsyncMock(), request=AsyncMock())
    with pytest.raises(ValueError, match="request.subject: payload 11 exceeds server limit 10"):
        opened.request("request.subject", b"x" * 11, 100)
    opened._nc.request.assert_not_awaited()
    opened.serve("service", "workers", lambda subject, data: b"x" * 11)
    callback = opened._nc.subscribe.await_args.kwargs["cb"]
    message = SimpleNamespace(subject="service", data=b"", reply="reply.subject", respond=AsyncMock())
    with pytest.raises(ValueError, match="reply.subject: payload 11 exceeds server limit 10"):
        asyncio.run(callback(message))
    message.respond.assert_not_awaited()


def test_scoped_page_write_refusal_and_missing_page_are_reported():
    client = SimpleNamespace(max_payload=2048)
    refusal = APIError(code=403, err_code=10072, description="page refused")

    async def publish(subject, *args, **kwargs):
        if "._chunks." in subject:
            raise refusal
        return SimpleNamespace(seq=1)

    js = SimpleNamespace(
        _nc=client,
        stream_info=AsyncMock(return_value=SimpleNamespace(config=SimpleNamespace(metadata={}))),
        publish=AsyncMock(side_effect=publish),
        get_last_msg=AsyncMock(side_effect=NotFoundError()),
    )
    root = SimpleNamespace(
        _nc=client, _js=js, _loop=SimpleNamespace(run=asyncio.run), _config=SimpleNamespace(provision=False)
    )
    store = ScopedStateStore(root, "crew", "work")
    with pytest.raises(APIError) as caught:
        store.create("large", random.Random(1).randbytes(4096))
    assert caught.value is refusal
    js.get_last_msg.side_effect = [
        SimpleNamespace(data=nats_payload.PAGED + b'["missing", 1]', headers={}, seq=1),
        NotFoundError(),
    ]
    with pytest.raises(ValueError, match="^state page '_chunks.missing.0' is missing$"):
        store.get("large")


@pytest.mark.parametrize("reconnect", [False, True])
def test_transport_configuration_is_checked_before_any_network_connection(monkeypatch, reconnect):
    from scripts.events.config import BusConfig

    async def connect(*args, **kwargs):
        assert args == ("nats://configured.example:4222",)
        expected = {"allow_reconnect": reconnect}
        if reconnect:
            expected["max_reconnect_attempts"] = -1
        assert kwargs == expected
        raise ValueError("configuration captured")

    monkeypatch.setattr(nats.nats_client, "connect", connect)
    with pytest.raises(ValueError, match="configuration captured"):
        nats.open_bus(BusConfig(backend="nats", url="nats://configured.example:4222", reconnect=reconnect))
