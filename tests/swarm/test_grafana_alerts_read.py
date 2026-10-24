import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from scripts.swarm import grafana_alerts

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("mcp-sdk")]

NOTE = {"id": 1, "newState": "Alerting", "text": "x {swarm=bottleneck}", "time": 1}


def _gateway(monkeypatch, body, error=False, hang=0):
    import mcp
    from mcp.client import streamable_http

    calls = []

    @asynccontextmanager
    async def transport(url, headers, timeout, httpx_client_factory):
        from scripts.swarm.langfuse_read import gateway_client

        assert httpx_client_factory is gateway_client
        assert url == "http://fixture/mcp/"
        assert headers == {
            "Authorization": "Bearer fixture",
            "Host": "llm.homeofanton.com",
            "x-mcp-servers": "grafana",
        }
        calls.append(("timeout", timeout))
        yield "read", "write", None

    class Session:
        def __init__(self, read, write):
            assert (read, write) == ("read", "write")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def initialize(self):
            import asyncio

            await asyncio.sleep(hang)
            calls.append("initialize")

        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return SimpleNamespace(
                isError=error,
                content=[SimpleNamespace(type="image", text="{}"), SimpleNamespace(type="text", text=json.dumps(body))],
            )

    monkeypatch.setenv("MCP_KEY_GATEWAY", "fixture")
    monkeypatch.setenv("LANGFUSE_MCP_URL", "http://fixture/mcp/")
    monkeypatch.setattr(streamable_http, "streamablehttp_client", transport)
    monkeypatch.setattr(mcp, "ClientSession", Session)
    return calls


def test_the_reader_asks_for_a_day_of_alert_state_history_through_the_gateway(monkeypatch):
    calls = _gateway(monkeypatch, {"Payload": [NOTE]})
    assert grafana_alerts.read_annotations(100_000_000, 0.5) == [NOTE]
    assert calls == [
        ("timeout", 0.5),
        "initialize",
        ("grafana-get_annotations", {"Type": "alert", "From": 100_000_000 - 86_400_000, "Limit": 1000}),
    ]


def test_the_reader_bounds_a_read_at_ten_seconds_by_default(monkeypatch):
    calls = _gateway(monkeypatch, {"Payload": []})
    assert grafana_alerts.read_annotations(100_000_000) == []
    assert calls[0] == ("timeout", 10)


@pytest.mark.parametrize("body", [{"Payload": None}, {}, [NOTE], None])
def test_the_reader_takes_an_empty_or_odd_reply_as_no_history(monkeypatch, body):
    _gateway(monkeypatch, body)
    assert grafana_alerts.read_annotations(100_000_000, 0.5) == []


def test_the_reader_gives_up_on_a_gateway_that_hangs_past_the_bound(monkeypatch):
    calls = _gateway(monkeypatch, {"Payload": [NOTE]}, hang=2)
    with pytest.raises(TimeoutError):
        grafana_alerts.read_annotations(100_000_000, 0.05)
    assert "initialize" not in calls


def test_the_reader_raises_on_a_tool_error(monkeypatch):
    _gateway(monkeypatch, {"Payload": [NOTE]}, error=True)
    with pytest.raises(RuntimeError, match="^Grafana annotations tool failed$"):
        grafana_alerts.read_annotations(100_000_000, 0.5)
