from types import SimpleNamespace

import pytest

from scripts.swarm import ledger_client
from scripts.swarm.store import SwarmError


def test_refused_plan_completion_names_invalid_slice(monkeypatch):
    response = {"rejected": ["finish"], "_meta": {"warnings": ["tasks/plan has invalid slice task ids: other"]}}
    monkeypatch.setattr(ledger_client, "_ledger", lambda: SimpleNamespace(call=lambda slug, ops: response))
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
    monkeypatch.setattr(ledger_client, "_ledger", lambda: SimpleNamespace(call=lambda slug, ops: response))
    with pytest.raises(ledger_client.LedgerRefused) as caught:
        ledger_client.LedgerClient().update_task("demo", "plan", {"state": "done"})
    assert str(caught.value) == expected


def test_accepted_state_is_returned_unchanged(monkeypatch):
    response = {"tasks": [], "_meta": {"warnings": ["Historical warning"]}, "rejected": []}
    monkeypatch.setattr(ledger_client, "_ledger", lambda: SimpleNamespace(call=lambda slug, ops: response))
    assert ledger_client.LedgerClient().state("demo") is response


@pytest.mark.feature("swarm-refusal-size-warnings")
@pytest.mark.parametrize(
    "reason",
    ["freeze_set refused: target tasks/missing does not exist", None],
    ids=["missing-target", "operation-fallback"],
)
def test_a_refused_focus_reports_size_warnings_separately(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], reason: str | None
) -> None:
    size = ["overview has 201 words, limit 200", "phase p113 description has 117 words, limit 100"]
    warnings = [size[0], *([reason] if reason else []), size[1]]
    response = {"rejected": ["focus"], "_meta": {"warnings": warnings}}
    monkeypatch.setattr(ledger_client, "_ledger", lambda: SimpleNamespace(call=lambda slug, ops: response))
    with pytest.raises(ledger_client.LedgerRefused) as caught:
        ledger_client.LedgerClient().freeze("demo", "focus", "tasks/missing")
    assert str(caught.value) == f"ledger demo refused: {reason or str(['focus'])}"
    captured = capsys.readouterr()
    assert captured.err.splitlines() == [f"ledger demo warning: {warning}" for warning in size]
    assert captured.out == ""
    assert response["_meta"]["warnings"] == warnings


@pytest.mark.feature("swarm-refusal-size-warnings")
def test_size_warnings_do_not_refuse_an_accepted_swarm_write(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    response = {"rejected": [], "_meta": {"warnings": ["phase p113 description has 117 words, limit 100"]}}
    monkeypatch.setattr(ledger_client, "_ledger", lambda: SimpleNamespace(call=lambda slug, ops: response))
    assert ledger_client.LedgerClient()._call("demo", [{"op": "freeze_set"}]) is response
    captured = capsys.readouterr()
    assert captured.err == captured.out == ""


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
    def call(slug, ops):
        raise SystemExit(exit_text)

    monkeypatch.setattr(ledger_client, "_ledger", lambda: SimpleNamespace(call=call, Missing=LookupError))
    with pytest.raises(SwarmError) as caught:
        ledger_client.LedgerClient().comment("demo", "t1", "text", by="engineer@1-2")
    assert isinstance(caught.value, ledger_client.LedgerRefused) is refused
    assert str(caught.value) == f"ledger demo: {exit_text}"
