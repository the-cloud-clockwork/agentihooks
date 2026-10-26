import pytest

from hooks.integrations import mailer
from hooks.integrations.mailer import EmailClient, EmailResult
from scripts.swarm import operator_email

ENV = {"AGENTIHOOKS_OPERATOR_EMAIL": "op@example.com", "MCP_KEY_GATEWAY": "k"}
SENT = {"status": "sent", "message_id": "m1"}


@pytest.fixture
def sent(monkeypatch):
    calls = {"smtp": [], "gateway": []}
    monkeypatch.setattr(mailer, "send_email", lambda *a, **k: calls["smtp"].append((a, k)) or EmailResult(True, 1))
    monkeypatch.setattr(operator_email, "gateway_send", lambda *a: calls["gateway"].append(a) or SENT)
    EmailClient.clear_cache()
    yield calls
    EmailClient.clear_cache()


def _reachable(monkeypatch, up):
    monkeypatch.setattr(EmailClient, "reachable", lambda self, timeout=3.0: up)


def test_a_reachable_relay_sends_once_through_smtp(sent, monkeypatch):
    _reachable(monkeypatch, True)
    assert operator_email.send("S", "T", ENV, ".") == ""
    assert sent == {"smtp": [(("op@example.com", "S"), {"body": "T"})], "gateway": []}


def test_an_unreachable_relay_sends_once_through_the_gateway_notifications_tool(sent, monkeypatch):
    _reachable(monkeypatch, False)
    assert operator_email.send("S", "T", ENV, ".") == ""
    assert sent == {
        "smtp": [],
        "gateway": [("https://10.10.30.200/mcp/", "k", {"to": "op@example.com", "subject": "S", "body": "T"})],
    }


def test_the_gateway_url_comes_from_the_session(sent, monkeypatch):
    _reachable(monkeypatch, False)
    env = {**ENV, "LANGFUSE_MCP_URL": "https://gateway.local/mcp/"}
    assert operator_email.send("S", "T", env, ".") == ""
    assert [call[0] for call in sent["gateway"]] == ["https://gateway.local/mcp/"]


def test_no_gateway_key_is_a_named_refusal(sent, monkeypatch):
    _reachable(monkeypatch, False)
    env = {"AGENTIHOOKS_OPERATOR_EMAIL": "op@example.com"}
    assert operator_email.send("S", "T", env, ".") == (
        "email failed: the SMTP relay is unreachable and MCP_KEY_GATEWAY is not set"
    )
    assert sent == {"smtp": [], "gateway": []}


def test_an_unsent_gateway_reply_is_reported(sent, monkeypatch):
    _reachable(monkeypatch, False)
    monkeypatch.setattr(operator_email, "gateway_send", lambda *a: sent["gateway"].append(a) or {"status": "queued"})
    assert operator_email.send("S", "T", ENV, ".") == 'email failed: the gateway answered {"status": "queued"}'
    assert len(sent["gateway"]) == 1


def test_a_gateway_error_is_reported(sent, monkeypatch):
    _reachable(monkeypatch, False)

    def down(*a):
        sent["gateway"].append(a)
        raise RuntimeError("401 Unauthorized")

    monkeypatch.setattr(operator_email, "gateway_send", down)
    assert operator_email.send("S", "T", ENV, ".") == (
        "email failed: the gateway notifications tool raised 401 Unauthorized"
    )
    assert len(sent["gateway"]) == 1


class _Item:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Result:
    def __init__(self, text, error=False):
        self.content = [_Item(text)]
        self.isError = error


def _gateway(monkeypatch, result):
    import contextlib

    import mcp
    from mcp.client import streamable_http

    calls = []

    @contextlib.asynccontextmanager
    async def client(url, **options):
        calls.append(("connect", url, options["headers"]))
        yield "read", "write", None

    class Session:
        def __init__(self, read, write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def initialize(self):
            calls.append("initialize")

        async def call_tool(self, name, arguments):
            calls.append(("call", name, arguments))
            return result

    monkeypatch.setattr(streamable_http, "streamablehttp_client", client)
    monkeypatch.setattr(mcp, "ClientSession", Session)
    return calls


def test_gateway_send_calls_the_notifications_tool_once_with_the_session_key(monkeypatch):
    calls = _gateway(monkeypatch, _Result('{"status": "sent", "message_id": "m1"}'))
    assert operator_email.gateway_send("https://gw/mcp/", "k", {"to": "a"}) == SENT
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
    _gateway(monkeypatch, _Result("refused: bad recipient", error=True))
    with pytest.raises(RuntimeError, match="refused: bad recipient"):
        operator_email.gateway_send("https://gw/mcp/", "k", {})


def _probe(monkeypatch, up):
    calls = []

    class Socket:
        def close(self):
            calls.append("closed")

    def connect(address, timeout):
        calls.append((address, timeout))
        if address[0] not in up:
            raise OSError("down")
        return Socket()

    monkeypatch.setattr(mailer.socket, "create_connection", connect)
    return calls


def test_reachable_probes_the_relay_name_first(monkeypatch):
    calls = _probe(monkeypatch, {"relay"})
    monkeypatch.setenv("SMTP_SERVER_IP", "10.0.0.9")
    assert EmailClient("relay", 25).reachable() is True
    assert calls == [(("relay", 25), 3.0), "closed"]


def test_reachable_falls_back_to_the_relay_address(monkeypatch):
    calls = _probe(monkeypatch, {"10.0.0.9"})
    monkeypatch.setenv("SMTP_SERVER_IP", "10.0.0.9")
    assert EmailClient("relay", 25).reachable(timeout=1.0) is True
    assert calls == [(("relay", 25), 1.0), (("10.0.0.9", 25), 1.0), "closed"]


def test_reachable_is_false_when_no_relay_answers(monkeypatch):
    calls = _probe(monkeypatch, set())
    monkeypatch.setenv("SMTP_SERVER_IP", "10.0.0.9")
    assert EmailClient("relay", 25).reachable() is False
    assert calls == [(("relay", 25), 3.0), (("10.0.0.9", 25), 3.0)]


def test_reachable_probes_only_the_relay_name_without_an_address(monkeypatch):
    calls = _probe(monkeypatch, set())
    monkeypatch.delenv("SMTP_SERVER_IP", raising=False)
    assert EmailClient("relay", 25).reachable() is False
    assert calls == [(("relay", 25), 3.0)]


def test_reachable_ignores_an_address_without_a_relay_name(monkeypatch):
    calls = _probe(monkeypatch, {"10.0.0.9"})
    monkeypatch.delenv("SMTP_SERVER", raising=False)
    monkeypatch.setenv("SMTP_SERVER_IP", "10.0.0.9")
    assert EmailClient().reachable() is False
    assert calls == []


def test_reachable_is_false_without_a_relay(monkeypatch):
    calls = _probe(monkeypatch, {"relay"})
    monkeypatch.delenv("SMTP_SERVER", raising=False)
    monkeypatch.delenv("SMTP_SERVER_IP", raising=False)
    assert EmailClient().reachable() is False
    assert calls == []
