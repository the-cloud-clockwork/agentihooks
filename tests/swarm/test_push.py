from unittest.mock import Mock

import pytest

from scripts.swarm import push

pytestmark = pytest.mark.unit


@pytest.fixture
def transport(monkeypatch):
    import requests

    monkeypatch.setenv("AGENTIHOOKS_PUSH_URL", "https://notifications.example/")
    monkeypatch.setenv("AGENTIHOOKS_PUSH_TOKEN", "synthetic")
    post = Mock(return_value=Mock())
    monkeypatch.setattr(requests, "post", post)
    return post


def test_push_uses_the_authenticated_notifications_route(transport):
    assert push.send("critical", "Ledger outage") is True
    transport.assert_called_once_with(
        "https://notifications.example/api/v1/push",
        headers={"Authorization": "Bearer synthetic"},
        json={"channel": "critical", "message": "Ledger outage"},
        timeout=5,
    )
    transport.return_value.raise_for_status.assert_called_once_with()


@pytest.mark.parametrize("missing", ["AGENTIHOOKS_PUSH_URL", "AGENTIHOOKS_PUSH_TOKEN"])
def test_missing_configuration_sends_nothing(transport, monkeypatch, missing):
    monkeypatch.delenv(missing)
    assert push.send("alerts", "Host pressure") is False
    transport.assert_not_called()


@pytest.mark.parametrize("failure", ["connection", "status"])
def test_push_failure_is_retryable_without_rendering_credentials(transport, capsys, failure):
    import requests

    error = requests.RequestException("synthetic")
    if failure == "connection":
        transport.side_effect = error
    else:
        transport.return_value.raise_for_status.side_effect = error
    assert push.send("alerts", "Host pressure") is False
    assert capsys.readouterr().err == "incident push failed; delivery will be retried\n"


def test_push_keeps_the_configured_route_prefix(transport, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_PUSH_URL", "https://notifications.example/proxyX/")
    assert push.send("alerts", "Host pressure") is True
    assert transport.call_args.args == ("https://notifications.example/proxyX/api/v1/push",)
