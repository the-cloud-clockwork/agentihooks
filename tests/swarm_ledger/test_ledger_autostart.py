from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import ledger

SLUG = "autostart-2026-01-01"


@pytest.fixture
def starts(monkeypatch, ledger_port):
    monkeypatch.setattr(ledger, "repository", SimpleNamespace(exists=lambda slug: True, read_page=lambda slug: ""))
    monkeypatch.setattr(ledger, "BASE", f"http://127.0.0.1:{ledger_port}")
    seen = []
    monkeypatch.setattr(ledger.subprocess, "run", lambda command, **kwargs: seen.append(command))
    return seen


def test_a_failed_request_on_a_fixed_port_starts_no_server_in_the_suite(starts):
    with pytest.raises(SystemExit, match="ledger server not answering on http://127.0.0.1:"):
        ledger.call(SLUG)
    assert starts == []


def test_a_failed_request_outside_the_suite_still_starts_the_server(starts, monkeypatch):
    monkeypatch.delenv("LEDGER_AUTOSTART", raising=False)
    with pytest.raises(SystemExit, match="ledger server not answering"):
        ledger.call(SLUG)
    assert [command[-1] for command in starts] == ["--ensure"]
