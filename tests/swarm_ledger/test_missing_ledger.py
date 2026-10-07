import pytest

from scripts.swarm import ledger_client
from scripts.swarm_ledger import ledger


def test_a_ledger_with_no_page_says_it_does_not_exist_without_starting_the_server(monkeypatch):
    started = []
    monkeypatch.setattr(ledger.subprocess, "run", lambda *a, **k: started.append(a))
    with pytest.raises(SystemExit, match="ledger gone-2026-01-01 does not exist"):
        ledger.call("gone-2026-01-01")
    assert started == []


def test_the_swarm_client_raises_ledger_gone_for_a_ledger_with_no_page(monkeypatch):
    monkeypatch.setattr(ledger_client, "_ledger", lambda: ledger)
    monkeypatch.setattr(ledger.subprocess, "run", lambda *a, **k: None)
    with pytest.raises(ledger_client.LedgerGone, match="ledger gone-2026-01-01 does not exist"):
        ledger_client.LedgerClient().state("gone-2026-01-01")
