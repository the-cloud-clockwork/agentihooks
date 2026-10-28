from unittest.mock import Mock

import pytest

from scripts.events import scheduler_state
from scripts.events.config import BusConfig
from scripts.scheduler.core import Scheduler

from . import nats_server

pytestmark = pytest.mark.unit


def test_scheduler_bus_is_shared_across_timers_and_closed_at_exit(monkeypatch):
    from scripts.events import factory

    scheduler_state._bus.cache_clear()
    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "bus")
    monkeypatch.setenv("AGENTIHOOKS_BUS", "memory")
    monkeypatch.delenv("AGENTIHOOKS_BUS_SCOPED", raising=False)
    bus = Mock()
    opened, closed = Mock(return_value=bus), Mock()
    monkeypatch.setattr(factory, "open_bus", opened)
    monkeypatch.setattr(scheduler_state, "atexit", Mock(register=closed))
    try:
        assert scheduler_state.open_scheduler_state("one") is bus.state.return_value
        assert scheduler_state.open_scheduler_state("two") is bus.state.return_value
        opened.assert_called_once_with(BusConfig.from_environ())
        assert [call.args for call in bus.state.call_args_list] == [("one",), ("two",)]
        closed.assert_called_once_with(bus.close)
    finally:
        scheduler_state._bus.cache_clear()


def test_scheduler_selects_its_swarm_on_a_scoped_bus(monkeypatch):
    from scripts.events import factory

    scheduler_state._bus.cache_clear()
    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "bus")
    monkeypatch.setenv("AGENTIHOOKS_BUS", "memory")
    monkeypatch.setenv("AGENTIHOOKS_BUS_SCOPED", "1")
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "scheduler-proof")
    bus = Mock()
    monkeypatch.setattr(factory, "open_bus", Mock(return_value=bus))
    monkeypatch.setattr(scheduler_state, "atexit", Mock())
    try:
        result = scheduler_state.open_scheduler_state("scheduler-member")
        bus.for_swarm.assert_called_once_with("scheduler-proof")
        bus.for_swarm.return_value.state.assert_called_once_with("scheduler-member")
        assert result is bus.for_swarm.return_value.state.return_value
    finally:
        scheduler_state._bus.cache_clear()


def test_scheduler_dispatches_against_real_nats_and_reuses_its_connection(tmp_path, monkeypatch):
    scheduler_state._bus.cache_clear()
    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "bus")
    monkeypatch.setenv("AGENTIHOOKS_BUS", "nats")
    monkeypatch.delenv("AGENTIHOOKS_BUS_SCOPED", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_BUS_BIND", raising=False)
    with nats_server.running(tmp_path / "jetstream") as url:
        monkeypatch.setenv("AGENTIHOOKS_BUS_URL", url)
        try:
            first, second = Scheduler(machine="nats-proof"), Scheduler(machine="nats-proof")
            assert first.state is second.state
            entry = first.timer(owner="proof", payload="ring", delay=1, now=10)
            entry.start(now=10)
            assert second.run_due(now=11) == 1
            assert entry.state == "fired"
            assert second.run_due(now=12) == 0
        finally:
            scheduler_state._bus(BusConfig.from_environ()).close()
            scheduler_state._bus.cache_clear()
