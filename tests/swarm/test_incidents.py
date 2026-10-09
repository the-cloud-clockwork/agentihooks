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


@pytest.mark.parametrize("load,memory", [(16.0, 64000), (0.5, 1)])
def test_host_pressure_pushes_and_mails_once_then_resolves(store, monkeypatch, load, memory):
    from scripts.swarm import cli, host_budget, push
    from tests.swarm.test_delivery import FakeHerdr
    from tests.swarm.test_tick import FakeLedger, FakeRuntime

    sent = Mock(return_value=True)
    monkeypatch.setattr(push, "send", sent)
    sample = host_budget.HostSample(load1=load, cpus=8, available_mb=memory, agents=2)
    monkeypatch.setattr(host_budget, "read_host", lambda: sample)
    ledger, runtime = FakeLedger([]), FakeRuntime()
    for _ in range(3):
        cli.run_tick(store, "sw", ledger, runtime, FakeHerdr({}))
    assert sent.call_count == 1
    assert sent.call_args.args[0] == "alerts"
    assert len(master_mail(store)) == 1
    sample = host_budget.HostSample(load1=0.5, cpus=8, available_mb=64000, agents=2)
    cli.run_tick(store, "sw", ledger, runtime, FakeHerdr({}))
    assert sent.call_count == 1
    cli.run_tick(store, "sw", ledger, runtime, FakeHerdr({}))
    assert sent.call_count == 2
    assert "resolved" in sent.call_args.args[1]
    assert len(master_mail(store)) == 2


def test_watchdog_outage_shares_probe_push_and_resolves(store, monkeypatch, tmp_path):
    from scripts.swarm import incidents, ledger_watchdog, push
    from tests.swarm.test_ledger_watchdog import PID, Host, plant
    from tests.swarm.test_tick import FakeRuntime

    sent = Mock(return_value=True)
    monkeypatch.setattr(push, "send", sent)
    host = Host(tmp_path, code=1, stderr="port busy")
    plant(host.proc, host.argv)
    (host.folder / ".server.pid").write_text(str(PID))
    clock = Clock()
    ledger = ProbedLedger(clock)
    incidents.step(store.redis, "ledger", True)
    ledger_watchdog.watch(store, "sw", ledger, FakeRuntime(), host)
    assert sent.call_count == 1
    assert sent.call_args.args[0] == "critical"
    assert "restart failed" in sent.call_args.args[1]
    assert len(master_mail(store)) == 1
    observe(store, ledger, clock)
    observe(store, ledger, clock)
    assert sent.call_count == 2
    assert "fast again" in sent.call_args.args[1]
    assert len(master_mail(store)) == 2
