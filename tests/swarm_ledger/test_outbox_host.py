import threading
from unittest.mock import Mock

import pytest

from scripts.events import factory, memory
from scripts.events.config import BusConfig
from scripts.swarm_ledger.repository import sqlite


@pytest.mark.wall_clock
def test_running_host_publishes_a_later_ledger_write_and_stops_its_relay(tmp_path, monkeypatch):
    from scripts.swarm_ledger import outbox_host

    repository = sqlite.SQLiteLedgerRepository(tmp_path / "ledgers.sqlite3")
    bus = memory.open_bus(BusConfig())
    published = threading.Event()
    publish = bus.events.publish

    def observed(subject, event):
        result = publish(subject, event)
        published.set()
        return result

    monkeypatch.setattr(bus.events, "publish", observed)
    close = Mock(wraps=bus.close)
    monkeypatch.setattr(bus, "close", close)
    opening = Mock(return_value=bus)
    monkeypatch.setattr(factory, "open_bus", opening)
    monkeypatch.setenv("AGENTIHOOKS_BUS", "nats")
    with outbox_host.running(repository, interval=0.01) as worker:
        assert worker.is_alive()
        repository.create("ledger", {"title": "Store", "phases": [{"title": "One"}]})
        assert published.wait(2), "the host did not publish the committed ledger change"
    assert not worker.is_alive()
    assert [subject for subject, _ in bus.events.entries] == ["swarm.ledger.phase.p1"]
    opening.assert_called_once_with(BusConfig(backend="nats"))
    close.assert_called_once_with()
    with repository.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM outbox_sent").fetchone() == (1,)


def test_memory_default_preserves_outbox_rows(tmp_path, monkeypatch):
    from scripts.swarm_ledger import outbox_host

    repository = sqlite.SQLiteLedgerRepository(tmp_path / "ledgers.sqlite3")
    repository.create("ledger", {"title": "Store", "phases": [{"title": "One"}]})
    monkeypatch.delenv("AGENTIHOOKS_BUS", raising=False)
    opening = Mock()
    monkeypatch.setattr(factory, "open_bus", opening)
    with outbox_host.running(repository) as worker:
        assert worker is None
    opening.assert_not_called()
    with repository.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM outbox_sent").fetchone() == (0,)


@pytest.mark.parametrize("failure", ["connect", "publish"])
def test_relay_retries_failures_on_the_next_interval_and_closes_its_bus(monkeypatch, capsys, failure):
    from scripts.swarm_ledger import outbox_host

    stopped = Mock()
    stopped.is_set.side_effect = [False, False, True]
    bus = Mock()
    opening = Mock(side_effect=[OSError("offline"), bus] if failure == "connect" else [bus, bus])
    monkeypatch.setattr(factory, "open_bus", opening)
    publish = Mock(side_effect=[OSError("offline"), 1] if failure == "publish" else [1])
    monkeypatch.setattr(outbox_host.outbox, "relay", publish)
    repository = object()
    config = BusConfig(backend="nats", url="nats://bus:4222")
    outbox_host.relay(repository, stopped, config, 0.25)
    assert opening.call_args_list == [((config,), {}), ((config,), {})]
    assert publish.call_args_list == [((repository, bus.events), {})] * (2 if failure == "publish" else 1)
    assert stopped.wait.call_args_list == [((0.25,), {}), ((0.25,), {})]
    assert bus.close.call_args_list == [((), {})] * (2 if failure == "publish" else 1)
    assert capsys.readouterr().err == "ledger bus relay: offline\n"


def test_relay_does_not_open_a_bus_after_stop_and_handles_failed_open(monkeypatch, capsys):
    from scripts.swarm_ledger import outbox_host

    opening = Mock(side_effect=OSError("offline"))
    monkeypatch.setattr(factory, "open_bus", opening)
    stopped = Mock()
    stopped.is_set.side_effect = [False, True]
    outbox_host.relay(object(), stopped, BusConfig(backend="nats"), 3)
    opening.assert_called_once_with(BusConfig(backend="nats"))
    stopped.wait.assert_called_once_with(3)
    assert capsys.readouterr().err == "ledger bus relay: offline\n"
    opening.reset_mock()
    stopped.is_set.side_effect = [True]
    outbox_host.relay(object(), stopped, BusConfig(backend="nats"), 3)
    opening.assert_not_called()


def test_host_signals_and_joins_worker_even_when_serving_raises(monkeypatch):
    from scripts.swarm_ledger import outbox_host

    monkeypatch.setenv("AGENTIHOOKS_BUS", "nats")
    stopped = Mock()
    monkeypatch.setattr(outbox_host.threading, "Event", Mock(return_value=stopped))
    worker = Mock()
    thread = Mock(return_value=worker)
    monkeypatch.setattr(outbox_host.threading, "Thread", thread)
    repository = object()
    with pytest.raises(ValueError, match="serving"):
        with outbox_host.running(repository) as running:
            assert running is worker
            worker.start.assert_called_once_with()
            stopped.set.assert_not_called()
            raise ValueError("serving")
    thread.assert_called_once_with(
        target=outbox_host.relay,
        args=(repository, stopped, BusConfig(backend="nats"), 1.0),
        daemon=True,
    )
    stopped.set.assert_called_once_with()
    worker.join.assert_called_once_with()


@pytest.mark.wall_clock
def test_ledger_server_alone_drives_the_outbox_until_shutdown(tmp_path, monkeypatch):
    from scripts.swarm_ledger import ledger_server

    repository = sqlite.SQLiteLedgerRepository(tmp_path / "ledgers.sqlite3")
    bus = memory.open_bus(BusConfig())
    published = threading.Event()
    publish = bus.events.publish

    def observed(subject, event):
        result = publish(subject, event)
        published.set()
        return result

    monkeypatch.setattr(bus.events, "publish", observed)
    close = Mock(wraps=bus.close)
    monkeypatch.setattr(bus, "close", close)
    monkeypatch.setattr(factory, "open_bus", Mock(return_value=bus))
    monkeypatch.setenv("AGENTIHOOKS_BUS", "nats")
    monkeypatch.setattr(ledger_server, "stored", repository)
    monkeypatch.setattr(ledger_server, "PIDFILE", tmp_path / "server.pid")
    monkeypatch.setattr(ledger_server.core, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(ledger_server.storage, "shared", lambda: None)
    monkeypatch.setattr(ledger_server.ports, "swarm", Mock(return_value=Mock()))
    monkeypatch.setattr(ledger_server.legacy, "adopt", Mock())
    for name in ("adopt_unrelayed", "metrics_journal", "watch_ledgers", "follow_shared"):
        monkeypatch.setattr(ledger_server, name, Mock())
    monkeypatch.setattr(ledger_server.server_code, "record", Mock())
    stopped = threading.Event()
    monkeypatch.setattr(ledger_server.server_lifetime, "watch", Mock(return_value=stopped))
    server = Mock()
    monkeypatch.setattr(ledger_server, "ThreadingHTTPServer", Mock(return_value=server))

    def serving():
        repository.create("ledger", {"title": "Store", "phases": [{"title": "One"}]})
        assert published.wait(3), "only the running server should drive publication"
        close.assert_not_called()

    server.serve_forever.side_effect = serving
    ledger_server.serve()
    close.assert_called_once_with()
    assert stopped.is_set()
    server.server_close.assert_called_once_with()
    assert [subject for subject, _ in bus.events.entries] == ["swarm.ledger.phase.p1"]


@pytest.mark.wall_clock
def test_a_disconnected_real_nats_client_is_reopened_and_the_row_reaches_a_consumer(tmp_path, monkeypatch):
    from scripts.swarm_ledger import outbox_host
    from tests.events import nats_server

    repository = sqlite.SQLiteLedgerRepository(tmp_path / "ledgers.sqlite3")
    opening = factory.open_bus
    opened = []

    def disconnect_first(config):
        bus = opening(config)
        if not opened:
            bus.close()
        opened.append(bus)
        return bus

    with nats_server.running(tmp_path / "jetstream") as url:
        monkeypatch.setenv("AGENTIHOOKS_BUS", "nats")
        monkeypatch.setenv("AGENTIHOOKS_BUS_URL", url)
        observer = opening(BusConfig(backend="nats", url=url))
        try:
            consumer = observer.events.consumer("host-proof", "swarm.>")
            monkeypatch.setattr(factory, "open_bus", disconnect_first)
            with outbox_host.running(repository, interval=0.01):
                repository.create("ledger", {"title": "Store", "phases": [{"title": "One"}]})
                deliveries = consumer.fetch(1, 3_000)
                assert [(delivery.event.entity, delivery.event.key) for delivery in deliveries] == [("phase", "p1")]
            assert len(opened) == 2
            assert all(bus._loop.closed for bus in opened)
            with repository.connect() as connection:
                assert connection.execute("SELECT COUNT(*) FROM outbox_sent").fetchone() == (1,)
        finally:
            observer.close()


def test_server_gives_its_repository_to_the_relay_and_stops_it_before_closing(monkeypatch, tmp_path):
    from contextlib import contextmanager

    from scripts.swarm_ledger import ledger_server, outbox_host

    lifecycle = []

    @contextmanager
    def running(repository):
        assert repository is ledger_server.stored
        lifecycle.append("relay started")
        try:
            yield
        finally:
            lifecycle.append("relay stopped")

    def serving():
        lifecycle.append("serving")
        raise ValueError("serving failed")

    monkeypatch.setattr(outbox_host, "running", running)
    monkeypatch.setattr(ledger_server, "PIDFILE", tmp_path / "server.pid")
    monkeypatch.setattr(ledger_server.core, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(ledger_server.storage, "shared", lambda: None)
    monkeypatch.setattr(ledger_server.ports, "swarm", Mock(return_value=Mock()))
    monkeypatch.setattr(ledger_server.legacy, "adopt", Mock())
    monkeypatch.setattr(ledger_server.threading, "Thread", Mock())
    for name in ("adopt_unrelayed", "metrics_journal"):
        monkeypatch.setattr(ledger_server, name, Mock())
    monkeypatch.setattr(ledger_server.server_code, "record", Mock())
    monkeypatch.setattr(ledger_server.server_lifetime, "watch", Mock())
    server = Mock()
    server.serve_forever.side_effect = serving
    server.server_close.side_effect = lambda: lifecycle.append("server closed")
    monkeypatch.setattr(ledger_server, "ThreadingHTTPServer", Mock(return_value=server))
    with pytest.raises(ValueError, match="serving failed"):
        ledger_server.serve()
    assert lifecycle == ["relay started", "serving", "relay stopped", "server closed"]
