import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("mcp-sdk")]


@pytest.mark.parametrize("outcome", ["success", "error", "timeout"])
def test_reader_uses_the_named_project_window_and_bounded_transport(monkeypatch, outcome):
    import mcp
    from mcp.client import streamable_http

    from scripts.swarm.langfuse_read import read_summary

    window = {"fromTimestamp": "2026-10-11T18:00:00Z", "toTimestamp": "2026-10-11T19:00:00Z"}
    payload = {"swarm": "rig-grade-swarm", "window": window, "agents": []}
    calls, initialized, closed = [], [], []

    @asynccontextmanager
    async def transport(url, headers, timeout):
        assert url == "http://fixture/mcp/"
        assert headers == {
            "Authorization": "Bearer fixture",
            "Host": "llm.homeofanton.com",
            "x-mcp-servers": "langfuse_tools",
        }
        assert timeout == 0.05
        try:
            yield "read", "write", None
        finally:
            closed.append(True)

    class Session:
        def __init__(self, read, write):
            assert (read, write) == ("read", "write")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def initialize(self):
            initialized.append(True)

        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            if outcome == "timeout":
                await asyncio.sleep(0.5)
            return SimpleNamespace(
                isError=outcome == "error",
                content=[SimpleNamespace(type="text", text=json.dumps(payload))],
            )

    monkeypatch.setenv("MCP_KEY_GATEWAY", "fixture")
    monkeypatch.setenv("LANGFUSE_MCP_URL", "http://fixture/mcp/")
    monkeypatch.setattr(streamable_http, "streamablehttp_client", transport)
    monkeypatch.setattr(mcp, "ClientSession", Session)
    if outcome == "success":
        assert read_summary("rig-grade-swarm", window, 0.05) == payload
    else:
        with pytest.raises(RuntimeError if outcome == "error" else TimeoutError) as error:
            read_summary("rig-grade-swarm", window, 0.05)
        if outcome == "error":
            assert str(error.value) == "Langfuse summary tool failed"
    assert calls == [
        (
            "langfuse_tools-swarm_error_latency_summary",
            {
                "swarm": "rig-grade-swarm",
                "project": "agent-swarm",
                "from_timestamp": window["fromTimestamp"],
                "to_timestamp": window["toTimestamp"],
            },
        )
    ]
    assert initialized == closed == [True]


def test_reader_requires_the_configured_endpoint_before_connecting(monkeypatch):
    from mcp.client import streamable_http

    from scripts.swarm.langfuse_read import read_summary

    calls = []

    def transport(*args, **kwargs):
        calls.append(args)
        raise AssertionError("An unconfigured read must not connect")

    monkeypatch.setenv("MCP_KEY_GATEWAY", "fixture")
    monkeypatch.delenv("LANGFUSE_MCP_URL", raising=False)
    monkeypatch.setattr(streamable_http, "streamablehttp_client", transport)
    with pytest.raises(KeyError) as error:
        read_summary("rig-grade-swarm", {}, 0.05)
    assert error.value.args == ("LANGFUSE_MCP_URL",)
    assert calls == []
