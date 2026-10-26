import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from scripts.swarm import operator_email

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("mcp-sdk")]


def _gateway(monkeypatch, text, error=False):
    import mcp
    from mcp.client import streamable_http

    calls = []

    @asynccontextmanager
    async def transport(url, headers, timeout, httpx_client_factory):
        calls.append(("connect", url, headers))
        yield "read", "write", None

    class Session:
        def __init__(self, read, write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def initialize(self):
            calls.append("initialize")

        async def call_tool(self, name, arguments):
            calls.append(("call", name, arguments))
            return SimpleNamespace(isError=error, content=[SimpleNamespace(type="text", text=text)])

    monkeypatch.setattr(streamable_http, "streamablehttp_client", transport)
    monkeypatch.setattr(mcp, "ClientSession", Session)
    return calls


def test_gateway_send_calls_the_notifications_tool_once_with_the_session_key(monkeypatch):
    reply = {"status": "sent", "message_id": "m1"}
    calls = _gateway(monkeypatch, json.dumps(reply))
    assert operator_email.gateway_send("https://gw/mcp/", "k", {"to": "a"}) == reply
    assert calls == [
        (
            "connect",
            "https://gw/mcp/",
            {"Authorization": "Bearer k", "Host": "llm.homeofanton.com", "x-mcp-servers": "notifications"},
        ),
        "initialize",
        ("call", "notifications-send_email", {"to": "a"}),
    ]


def test_gateway_send_raises_the_tool_error_text(monkeypatch):
    _gateway(monkeypatch, "refused: bad recipient", error=True)
    with pytest.raises(RuntimeError, match="refused: bad recipient"):
        operator_email.gateway_send("https://gw/mcp/", "k", {})
