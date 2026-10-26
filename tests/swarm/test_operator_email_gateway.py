import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from scripts.swarm import operator_email

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("mcp-sdk")]


def _gateway(monkeypatch, content, error=False, delay=0.0):
    import mcp
    from mcp.client import streamable_http

    from scripts.swarm.langfuse_read import gateway_client

    calls = []

    @asynccontextmanager
    async def transport(url, headers, timeout, httpx_client_factory):
        assert httpx_client_factory is gateway_client
        calls.append(("connect", url, headers, timeout))
        yield "read", "write", None

    class Session:
        def __init__(self, read, write):
            assert (read, write) == ("read", "write")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def initialize(self):
            calls.append("initialize")

        async def call_tool(self, name, arguments):
            calls.append(("call", name, arguments))
            await asyncio.sleep(delay)
            items = [SimpleNamespace(type="text", text=text) for text in content]
            return SimpleNamespace(isError=error, content=items)

    monkeypatch.setattr(streamable_http, "streamablehttp_client", transport)
    monkeypatch.setattr(mcp, "ClientSession", Session)
    return calls


def test_gateway_send_calls_the_notifications_tool_once_with_the_session_key(monkeypatch):
    reply = {"status": "sent", "message_id": "m1"}
    calls = _gateway(monkeypatch, [json.dumps(reply)])
    assert operator_email.gateway_send("https://gw/mcp/", "k", {"to": "a"}) == reply
    assert calls == [
        (
            "connect",
            "https://gw/mcp/",
            {"Authorization": "Bearer k", "Host": "llm.homeofanton.com", "x-mcp-servers": "notifications"},
            10,
        ),
        "initialize",
        ("call", "notifications-send_email", {"to": "a"}),
    ]


def test_gateway_send_raises_the_tool_error_text(monkeypatch):
    _gateway(monkeypatch, ["refused: bad recipient"], error=True)
    with pytest.raises(RuntimeError, match="refused: bad recipient"):
        operator_email.gateway_send("https://gw/mcp/", "k", {})


def test_gateway_send_names_an_error_without_text(monkeypatch):
    _gateway(monkeypatch, [], error=True)
    with pytest.raises(RuntimeError, match="^an error$"):
        operator_email.gateway_send("https://gw/mcp/", "k", {})


def test_gateway_send_reads_a_reply_without_text_as_empty(monkeypatch):
    _gateway(monkeypatch, [])
    assert operator_email.gateway_send("https://gw/mcp/", "k", {}) == {}


def test_gateway_send_raises_on_a_reply_that_is_not_an_object(monkeypatch):
    _gateway(monkeypatch, ['["sent"]'])
    with pytest.raises(RuntimeError, match=r'a reply that is not an object: \["sent"\]'):
        operator_email.gateway_send("https://gw/mcp/", "k", {})


def test_gateway_send_is_bounded_by_its_timeout(monkeypatch):
    monkeypatch.setattr(operator_email, "TIMEOUT_S", 0.05)
    _gateway(monkeypatch, ["{}"], delay=0.5)
    with pytest.raises(TimeoutError):
        operator_email.gateway_send("https://gw/mcp/", "k", {})
