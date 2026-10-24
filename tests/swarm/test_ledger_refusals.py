from types import SimpleNamespace

import pytest

from scripts.swarm import ledger_client
from scripts.swarm.store import SwarmError


def test_refused_plan_completion_names_invalid_slice(monkeypatch):
    response = {"rejected": ["finish"], "_meta": {"warnings": ["tasks/plan has invalid slice task ids: other"]}}
    monkeypatch.setattr(ledger_client, "_ledger", lambda: SimpleNamespace(call=lambda slug, ops, service: response))
    with pytest.raises(SwarmError, match="invalid slice task ids: other"):
        ledger_client.LedgerClient().update_task("demo", "plan", {"state": "done", "proof": {"slice": "other"}})


@pytest.mark.parametrize(
    "response, expected",
    [
        (
            {"rejected": ["finish"], "_meta": {"warnings": ["bad slice: other", "bad slice: unknown"]}},
            "ledger demo refused: bad slice: other; bad slice: unknown",
        ),
        ({"rejected": ["finish"], "_meta": {"warnings": []}}, "ledger demo refused: ['finish']"),
        ({"rejected": ["finish"], "_meta": {}}, "ledger demo refused: ['finish']"),
        ({"rejected": ["finish"]}, "ledger demo refused: ['finish']"),
    ],
)
def test_refusal_keeps_diagnostics_or_operation_fallback(monkeypatch, response, expected):
    monkeypatch.setattr(ledger_client, "_ledger", lambda: SimpleNamespace(call=lambda slug, ops, service: response))
    with pytest.raises(ledger_client.LedgerRefused) as caught:
        ledger_client.LedgerClient().update_task("demo", "plan", {"state": "done"})
    assert str(caught.value) == expected


def test_accepted_state_is_returned_unchanged(monkeypatch):
    response = {"tasks": [], "_meta": {"warnings": ["Historical warning"]}, "rejected": []}
    monkeypatch.setattr(ledger_client, "_ledger", lambda: SimpleNamespace(call=lambda slug, ops, service: response))
    assert ledger_client.LedgerClient().state("demo") is response


@pytest.mark.parametrize(
    ("exit_text", "refused"),
    [
        ('server refused: 400 {"error": {"code": "schema_invalid"}}', True),
        ('server refused: 409 {"error": {"code": "conflict"}}', True),
        ("server refused: 503 unavailable", False),
        ("ledger server not answering", False),
    ],
)
def test_a_client_error_from_the_server_is_a_ledger_refusal(monkeypatch, exit_text, refused):
    def call(slug, ops, service):
        raise SystemExit(exit_text)

    monkeypatch.setattr(ledger_client, "_ledger", lambda: SimpleNamespace(call=call, Missing=LookupError))
    with pytest.raises(SwarmError) as caught:
        ledger_client.LedgerClient().comment("demo", "t1", "text", by="engineer@1-2")
    assert isinstance(caught.value, ledger_client.LedgerRefused) is refused
    assert str(caught.value) == f"ledger demo: {exit_text}"
