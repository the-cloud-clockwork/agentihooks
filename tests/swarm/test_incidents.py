from unittest.mock import Mock

import pytest

from scripts.swarm import ledger_probe
from tests.swarm.test_ledger_probe import Clock, ProbedLedger, master_mail, observe
from tests.swarm.test_ledger_probe import store as store

pytestmark = pytest.mark.unit


def test_slow_probe_pushes_once_and_resolves_after_two_fast_passes(store, monkeypatch):
    from scripts.swarm import push

    sent = Mock(return_value=True)
    monkeypatch.setattr(push, "send", sent)
    clock = Clock()
    ledger = ProbedLedger(clock, read_s=6.0)
    observe(store, ledger, clock)
    assert sent.call_count == 0
    observe(store, ledger, clock)
    observe(store, ledger, clock)
    assert sent.call_count == 1
    assert sent.call_args.args[0] == "critical"
    assert "slow" in sent.call_args.args[1]
    assert len(master_mail(store)) == 1
    ledger.read_s = 0.1
    observe(store, ledger, clock)
    assert sent.call_count == 1
    observe(store, ledger, clock)
    assert sent.call_count == 2
    assert "fast again" in sent.call_args.args[1]
    assert len(master_mail(store)) == 2
    assert not ledger_probe.holding(store, "sw")
