import httpx
import pytest

from hooks.integrations import mailer
from hooks.integrations.mailer import EmailClient, EmailResult
from scripts.swarm import operator_email

ENV = {"AGENTIHOOKS_OPERATOR_EMAIL": "op@example.com", "NOTIFICATIONS_API_KEY": "k"}


class Answer:
    def __init__(self, status_code):
        self.status_code = status_code


@pytest.fixture
def sent(monkeypatch):
    calls = {"smtp": [], "api": []}
    monkeypatch.setattr(mailer, "send_email", lambda *a, **k: calls["smtp"].append((a, k)) or EmailResult(True, 1))
    monkeypatch.setattr(httpx, "post", lambda *a, **k: calls["api"].append((a, k)) or Answer(200))
    EmailClient.clear_cache()
    yield calls
    EmailClient.clear_cache()


def _reachable(monkeypatch, up):
    monkeypatch.setattr(EmailClient, "reachable", lambda self, timeout=3.0: up)


def test_a_reachable_relay_sends_once_through_smtp(sent, monkeypatch):
    _reachable(monkeypatch, True)
    assert operator_email.send("S", "T", ENV, ".") == ""
    assert sent == {"smtp": [(("op@example.com", "S"), {"body": "T"})], "api": []}


def test_an_unreachable_relay_sends_once_through_the_notifications_api(sent, monkeypatch):
    _reachable(monkeypatch, False)
    assert operator_email.send("S", "T", ENV, ".") == ""
    assert sent == {
        "smtp": [],
        "api": [
            (
                ("https://email.homeofanton.com/api/v1/send",),
                {
                    "headers": {"Authorization": "Bearer k"},
                    "json": {"to": "op@example.com", "subject": "S", "body": "T"},
                    "timeout": 10,
                },
            )
        ],
    }


def test_the_notifications_url_is_overridable(sent, monkeypatch):
    _reachable(monkeypatch, False)
    env = {**ENV, "AGENTIHOOKS_NOTIFICATIONS_URL": "http://notify.local/send"}
    assert operator_email.send("S", "T", env, ".") == ""
    assert [call[0] for call in sent["api"]] == [("http://notify.local/send",)]


def test_no_reachable_transport_is_a_named_refusal(sent, monkeypatch):
    _reachable(monkeypatch, False)
    env = {"AGENTIHOOKS_OPERATOR_EMAIL": "op@example.com"}
    assert operator_email.send("S", "T", env, ".") == (
        "email failed: the SMTP relay is unreachable and NOTIFICATIONS_API_KEY is not set"
    )
    assert sent == {"smtp": [], "api": []}


def test_a_refused_api_answer_is_reported(sent, monkeypatch):
    _reachable(monkeypatch, False)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: sent["api"].append(a) or Answer(500))
    assert operator_email.send("S", "T", ENV, ".") == "email failed: notifications answered 500"
    assert len(sent["api"]) == 1


def test_an_api_transport_error_is_reported(sent, monkeypatch):
    _reachable(monkeypatch, False)

    def down(*a, **k):
        sent["api"].append(a)
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(httpx, "post", down)
    assert operator_email.send("S", "T", ENV, ".") == "email failed: no route"
    assert len(sent["api"]) == 1


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


def test_reachable_is_false_without_a_relay(monkeypatch):
    calls = _probe(monkeypatch, {"relay"})
    monkeypatch.delenv("SMTP_SERVER", raising=False)
    monkeypatch.delenv("SMTP_SERVER_IP", raising=False)
    assert EmailClient().reachable() is False
    assert calls == []
