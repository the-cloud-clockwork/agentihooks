import pytest

from scripts.swarm.ledger_client import LedgerClient, _ledger
from scripts.swarm.tick import tick
from tests.swarm.test_lifetime import idle_master
from tests.swarm.test_tick import FakeLedger, FakeRuntime
from tests.swarm.test_tick import store as store

pytestmark = pytest.mark.xdist_group("fakeredis")


class ClosingLedger(FakeLedger):
    def __init__(self, closed_at):
        super().__init__([])
        self.closed_at, self.binned = closed_at, []

    def state(self, slug):
        return {**super().state(slug), "closed_at": self.closed_at}

    def bin_closed(self, slug, closed_at):
        self.binned.append((slug, closed_at))
        return True


def test_a_closed_ledger_goes_to_the_bin_once_its_swarm_stopped_and_its_space_closed(store):
    store.update("sw", state="stopped")
    ledger, rt = ClosingLedger(5), FakeRuntime()
    actions = tick("sw", store, ledger, rt, 10)
    assert rt.closed_spaces == ["sw"]
    assert ledger.binned == [("sw", 5)]
    assert "the closed ledger moved to the bin" in actions


def test_an_open_ledger_stays_out_of_the_bin(store):
    store.update("sw", state="stopped")
    ledger = ClosingLedger(None)
    tick("sw", store, ledger, FakeRuntime(), 10)
    assert ledger.binned == []


def test_a_closed_ledger_waits_for_its_master_to_leave(store):
    _, rt = idle_master(store, "stopped")
    ledger = ClosingLedger(5)
    tick("sw", store, ledger, rt, 10)
    assert ledger.binned == []


def test_the_client_bins_a_closed_ledger_and_reopen_takes_it_out(monkeypatch):
    _ledger()
    import ledger_bin

    calls, client = [], LedgerClient()
    monkeypatch.setattr(client, "_call", lambda slug, ops=None: calls.append(ops[0]["op"]) or {})
    monkeypatch.setattr(ledger_bin, "bin_closed", lambda slug, closed_at: calls.append(("bin", slug, closed_at)))
    monkeypatch.setattr(ledger_bin, "restore", lambda slug: calls.append(("restore", slug)))
    client.bin_closed("sw", 5)
    client.reopen("sw", "operator")
    assert calls == [("bin", "sw", 5), "reopen", ("restore", "sw")]
