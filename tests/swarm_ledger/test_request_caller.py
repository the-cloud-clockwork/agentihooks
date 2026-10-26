from email.message import Message
from types import SimpleNamespace

import pytest


def headers(**pairs):
    found = Message()
    for key, value in pairs.items():
        found[key.replace("_", "-")] = value
    return found


@pytest.mark.parametrize(
    ("given", "caller"),
    [
        (None, "-"),
        (headers(), "-"),
        (headers(X_Ledger_Agent="engineer@1-1"), "engineer@1-1"),
        (headers(X_Ledger_Agent="engineer@1-1", X_Controller_Credential="c"), "engineer@1-1"),
        (headers(X_Ledger_Agent="", X_Controller_Credential="c"), "controller"),
        (headers(X_Controller_Credential="c"), "controller"),
    ],
)
def test_the_request_caller_names_the_agent_then_the_controller(given, caller):
    from scripts.swarm_ledger import ledger_server

    assert ledger_server.request_caller(given) == caller


def test_an_api_request_log_line_carries_its_caller(capsys, monkeypatch):
    from scripts.swarm_ledger import ledger_server

    monkeypatch.setattr(ledger_server.time, "strftime", lambda fmt: f"<{fmt}>")
    handler = SimpleNamespace(
        path="/api/v1/ledgers/demo/tasks?limit=100", command="GET", headers=headers(X_Ledger_Agent="engineer@1-1")
    )
    ledger_server.Handler.log_message(handler, '"%s" %s', "GET", "200")
    assert capsys.readouterr().err == "<%H:%M:%S> GET /api/v1/ledgers/demo/tasks engineer@1-1\n"


def test_an_api_request_without_parsed_headers_logs_no_caller(capsys, monkeypatch):
    from scripts.swarm_ledger import ledger_server

    monkeypatch.setattr(ledger_server.time, "strftime", lambda fmt: "t")
    ledger_server.Handler.log_message(SimpleNamespace(path="/api/v1/ledgers", command="GET"), "%s", "x")
    assert capsys.readouterr().err == "t GET /api/v1/ledgers -\n"
