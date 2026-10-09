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


def test_two_swarms_share_one_push_and_each_master_gets_one_item(store, monkeypatch):
    from scripts.inbox.store import InboxStore
    from scripts.swarm import host_budget, incidents, push
    from scripts.swarm.store import SwarmConfig

    store.create(SwarmConfig("other", "/repo", max_eng=1, max_ci=0))
    sent = Mock(return_value=True)
    monkeypatch.setattr(push, "send", sent)
    sample = host_budget.HostSample(load1=16, cpus=8, available_mb=64000, agents=2)
    monkeypatch.setattr(host_budget, "read_host", lambda: sample)
    for _ in range(3):
        incidents.host_pressure(store, "sw")
        incidents.host_pressure(store, "other")
    assert sent.call_count == 1
    assert len(master_mail(store)) == 1
    assert len(InboxStore(store.redis).pending_items("master@other")) == 1
    sample = host_budget.HostSample(load1=0.5, cpus=8, available_mb=64000, agents=2)
    for _ in range(3):
        incidents.host_pressure(store, "sw")
        incidents.host_pressure(store, "other")
    assert sent.call_count == 2
    assert len(master_mail(store)) == 2
    assert len(InboxStore(store.redis).pending_items("master@other")) == 2


def test_failed_push_is_retried_and_recovery_keeps_its_order(store, monkeypatch):
    from scripts.swarm import incidents, push

    sent = Mock(side_effect=[False, True, True])
    monkeypatch.setattr(push, "send", sent)
    assert incidents.step(store.redis, "ledger", True) == ""
    assert incidents.step(store.redis, "ledger", True) == "raised"
    incidents.deliver(store.redis, "ledger", "outage", "recovered")
    assert incidents.step(store.redis, "ledger", False) == ""
    assert incidents.step(store.redis, "ledger", False) == "resolved"
    incidents.deliver(store.redis, "ledger", "outage", "recovered")
    incidents.deliver(store.redis, "ledger", "outage", "recovered")
    incidents.deliver(store.redis, "ledger", "outage", "recovered")
    assert [call.args for call in sent.call_args_list] == [
        ("critical", "outage"),
        ("critical", "outage"),
        ("critical", "recovered"),
    ]


def test_unknown_host_does_not_resolve_pressure_and_read_failure_keeps_the_tick_running(store, monkeypatch, capsys):
    from scripts.swarm import cli, host_budget, incidents, push
    from tests.swarm.test_delivery import FakeHerdr
    from tests.swarm.test_tick import FakeLedger, FakeRuntime

    sent = Mock(return_value=True)
    monkeypatch.setattr(push, "send", sent)
    monkeypatch.setattr(host_budget, "read_host", lambda: host_budget.HostSample(16, 8, 64000, 2))
    incidents.host_pressure(store, "sw")
    incidents.host_pressure(store, "sw")
    monkeypatch.setattr(host_budget, "read_host", lambda: None)
    incidents.host_pressure(store, "sw")
    incidents.host_pressure(store, "sw")
    assert sent.call_count == 1 and len(master_mail(store)) == 1

    readings = iter([None, host_budget.HostSample(0.5, 8, 64000, 2)])

    def unreadable():
        sample = next(readings, host_budget.HostSample(0.5, 8, 64000, 2))
        if sample is None:
            raise OSError("host unavailable")
        return sample

    monkeypatch.setattr(host_budget, "read_host", unreadable)
    cli.run_tick(store, "sw", FakeLedger([]), FakeRuntime(), FakeHerdr({}))
    assert store.redis.get(store.key("sw", "last-tick")) is not None
    assert "host pressure check unavailable" in capsys.readouterr().err


def test_concurrent_deliveries_send_one_push(store, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from scripts.swarm import incidents, push

    entered, release = Event(), Event()

    def send(channel, text):
        entered.set()
        assert release.wait(5)
        return True

    sent = Mock(side_effect=send)
    monkeypatch.setattr(push, "send", sent)
    incidents.step(store.redis, "ledger", True)
    incidents.step(store.redis, "ledger", True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        delivery = pool.submit(incidents.deliver, store.redis, "ledger", "outage", "recovered")
        assert entered.wait(5)
        try:
            incidents.deliver(store.redis, "ledger", "outage", "recovered")
        finally:
            release.set()
        delivery.result(timeout=5)
    incidents.deliver(store.redis, "ledger", "outage", "recovered")
    assert sent.call_count == 1


@pytest.mark.parametrize("bad_ticks", [1, 3])
def test_watchdog_failures_and_fast_probes_form_one_incident_per_tick(store, monkeypatch, tmp_path, bad_ticks):
    from scripts.swarm import cli, ledger_host, ledger_watchdog, push
    from tests.swarm.test_delivery import FakeHerdr
    from tests.swarm.test_ledger_watchdog import PID, Host, current, plant
    from tests.swarm.test_tick import FakeRuntime

    sent = Mock(return_value=True)
    monkeypatch.setattr(push, "send", sent)
    monkeypatch.setattr(ledger_host, "facts", lambda: {})
    host = Host(tmp_path)
    plant(host.proc, host.argv)
    (host.folder / ".server.pid").write_text(str(PID))

    def refused(pid, sig):
        raise PermissionError("restart refused")

    host.kill = refused
    monkeypatch.setattr(ledger_watchdog, "default_host", lambda: host)
    ledger, runtime = ProbedLedger(Clock()), FakeRuntime()
    for _ in range(bad_ticks):
        cli.run_tick(store, "sw", ledger, runtime, FakeHerdr({}))
    expected = 0 if bad_ticks == 1 else 1
    assert sent.call_count == expected
    assert len(master_mail(store)) == expected
    current(host, tmp_path)
    cli.run_tick(store, "sw", ledger, runtime, FakeHerdr({}))
    assert sent.call_count == expected
    cli.run_tick(store, "sw", ledger, runtime, FakeHerdr({}))
    assert sent.call_count == expected * 2
    assert len(master_mail(store)) == expected * 2
