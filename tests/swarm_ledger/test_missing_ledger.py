import pytest

from scripts.swarm import ledger_client
from scripts.swarm_ledger import ledger


def test_a_ledger_with_no_record_says_it_does_not_exist() -> None:
    with pytest.raises(ledger.Missing, match="ledger gone-2026-01-01 does not exist"):
        ledger.call("gone-2026-01-01")


def test_the_swarm_client_raises_ledger_gone_for_a_ledger_with_no_record(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ledger_client, "_ledger", lambda: ledger)
    with pytest.raises(ledger_client.LedgerGone, match="ledger gone-2026-01-01 does not exist"):
        ledger_client.LedgerClient().state("gone-2026-01-01")
