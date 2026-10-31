import threading
from contextlib import nullcontext
from unittest.mock import Mock

import pytest

from scripts.events import factory, memory
from scripts.events.config import BusConfig
from scripts.hub import projector
from scripts.swarm_ledger.repository import outbox, sqlite


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
    with outbox_host.running(repository, interval=0.01):
        repository.create("ledger", {"title": "Store", "phases": [{"title": "One"}]})
        assert published.wait(2), "the host did not publish the committed ledger change"
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


def test_relay_retries_failures_on_the_next_interval_and_closes_its_bus(monkeypatch, capsys, failure):
    from scripts.swarm_ledger import outbox_host

    bus = Mock()
    opening = Mock(side_effect=[OSError("offline"), bus] if failure == "connect" else [bus, bus])
    monkeypatch.setattr(factory, "open_bus", opening)
    publish = Mock(side_effect=[OSError("offline"), 1, 1] if failure == "publish" else [1, 1])
    monkeypatch.setattr(outbox_host.outbox, "relay", publish)
    prune = Mock(return_value=0)
    monkeypatch.setattr(outbox_host.outbox, "prune", prune)
    repository = object()
    config = BusConfig(backend="nats", url="nats://bus:4222", dedupe_window_ms=7_000)
    relay = outbox_host.Relay(repository, config)
    relay.publish()
    assert relay.bus is None
    relay.publish()
    assert relay.bus is bus
    relay.publish()
    relay.close()
    relay.close()
    assert relay.bus is None
    assert opening.call_args_list == [((config,), {}), ((config,), {})]
    assert publish.call_args_list == [((repository, bus.events), {})] * (3 if failure == "publish" else 2)
    prune.assert_not_called()
    assert bus.close.call_args_list == [((), {})] * (2 if failure == "publish" else 1)
    assert capsys.readouterr().err == "ledger bus relay: offline\n"


def test_closing_a_relay_without_a_connection_is_safe(monkeypatch):
    from scripts.swarm_ledger import outbox_host

    opening = Mock()
    monkeypatch.setattr(factory, "open_bus", opening)
    relay = outbox_host.Relay(object(), BusConfig(backend="nats"))
    relay.close()
    relay.close()
    assert relay.bus is None
    opening.assert_not_called()


def test_host_stops_the_scheduled_interval_before_closing_the_bus(monkeypatch, close_error):
    from scripts.swarm_ledger import outbox_host

    monkeypatch.setenv("AGENTIHOOKS_BUS", "nats")
    pruning, relaying = Mock(), Mock()
    interval = Mock(side_effect=[pruning, relaying])
    monkeypatch.setattr(outbox_host, "Interval", interval)
    bus = Mock()
    opening = Mock(return_value=bus)
    monkeypatch.setattr(factory, "open_bus", opening)
    monkeypatch.setattr(outbox_host.outbox, "relay", Mock())
    monkeypatch.setattr(outbox_host.outbox, "prune", Mock())
    relaying.close.side_effect = ValueError("stop") if close_error else None
    repository = object()
    with pytest.raises(ValueError, match="stop" if close_error else "serving"):
        with outbox_host.running(repository):
            pruning.start.assert_called_once_with()
            relaying.start.assert_called_once_with()
            relaying.close.assert_not_called()
            callback = interval.call_args.args[0]
            callback()
            bus.close.assert_not_called()
            raise ValueError("serving")
    assert [call.kwargs for call in interval.call_args_list] == [{"every": 30.0}, {"every": 1.0}]
    relaying.close.assert_called_once_with()
    pruning.close.assert_called_once_with()
    bus.close.assert_called_once_with()
    assert callback.__self__.bus is None
    assert callback.__self__.repository is repository
    opening.assert_called_once_with(BusConfig(backend="nats"))


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
    monkeypatch.setattr(projector, "running", Mock(return_value=nullcontext()))
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

    def serving(poll_interval):
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


def test_server_gives_the_relay_and_the_projector_their_stores_and_stops_them_before_closing(monkeypatch, tmp_path):
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

    @contextmanager
    def projecting(repository, hub):
        assert (repository, hub) == (ledger_server.repository, ledger_server.HUB)
        lifecycle.append("projector started")
        try:
            yield
        finally:
            lifecycle.append("projector stopped")

    def serving(poll_interval):
        assert poll_interval == ledger_server.POLL_S
        lifecycle.append("serving")
        raise ValueError("serving failed")

    monkeypatch.setattr(outbox_host, "running", running)
    monkeypatch.setattr(projector, "running", projecting)
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
    server.drained.side_effect = lambda timeout: lifecycle.append(("drained", timeout)) or True
    monkeypatch.setattr(ledger_server, "ThreadingHTTPServer", Mock(return_value=server))
    with pytest.raises(ValueError, match="serving failed"):
        ledger_server.serve()
    assert lifecycle == [
        "relay started",
        "projector started",
        "serving",
        ("drained", ledger_server.DRAIN_S),
        "projector stopped",
        "relay stopped",
        "server closed",
    ]


def test_a_relay_that_fails_to_start_releases_the_lease_without_serving(monkeypatch, tmp_path, capsys):
    from scripts.swarm_ledger import ledger_server, outbox_host

    def broken(repository):
        raise RuntimeError("relay refused")

    monkeypatch.setattr(outbox_host, "running", broken)
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
    monkeypatch.setattr(ledger_server, "ThreadingHTTPServer", Mock(return_value=server))
    with pytest.raises(RuntimeError, match="relay refused"):
        ledger_server.serve()
    server.serve_forever.assert_not_called()
    server.server_close.assert_called_once_with()
    ledger_server.ports.swarm.return_value.ledger_writer.WriterLease.return_value.release.assert_called_once_with()
    assert "drain:" not in capsys.readouterr().err


def test_the_host_prune_drops_sent_rows_outside_the_dedupe_window_and_keeps_the_rest(tmp_path, monkeypatch):
    from scripts.swarm_ledger import outbox_host

    repository = sqlite.SQLiteLedgerRepository(tmp_path / "ledgers.sqlite3")
    bus = memory.open_bus(BusConfig())
    config = BusConfig(backend="nats")

    def ids():
        with repository.connect() as connection:
            return {row[0] for row in connection.execute("SELECT id FROM outbox").fetchall()}

    def marks():
        with repository.connect() as connection:
            return {row[0] for row in connection.execute("SELECT id FROM outbox_sent").fetchall()}

    repository.create("old", {"title": "Old", "phases": [{"title": "One"}]})
    stale = ids()
    outbox.relay(repository, bus.events, clock=lambda: 1_000)
    repository.create("recent", {"title": "Recent", "phases": [{"title": "One"}]})
    recent = ids() - stale
    outbox.relay(repository, bus.events, clock=lambda: outbox.now_ms() - config.dedupe_window_ms // 2)
    repository.create("unsent", {"title": "Unsent", "phases": [{"title": "One"}]})
    unsent = ids() - stale - recent
    assert [len(stale), len(recent), len(unsent)] == [1, 1, 1]
    opening = Mock()
    monkeypatch.setattr(factory, "open_bus", opening)
    outbox_host.prune(repository, config)
    assert ids() == recent | unsent
    assert marks() == recent
    opening.assert_not_called()


def test_the_host_prune_caps_unsent_rows_at_the_outbox_limit(monkeypatch):
    from scripts.swarm_ledger import outbox_host

    prune, cap = Mock(return_value=0), Mock(return_value=0)
    monkeypatch.setattr(outbox_host.outbox, "prune", prune)
    monkeypatch.setattr(outbox_host.outbox, "cap", cap)
    monkeypatch.setattr(outbox_host.outbox, "MAX_UNSENT", 7)
    repository = object()
    outbox_host.prune(repository, BusConfig(dedupe_window_ms=9_000))
    prune.assert_called_once_with(repository, 9_000)
    cap.assert_called_once_with(repository, 7)


def test_a_failed_prune_logs_one_line_and_leaves_the_interval_running(monkeypatch, capsys):
    from scripts.swarm_ledger import outbox_host

    monkeypatch.setattr(outbox_host.outbox, "prune", Mock(side_effect=OSError("disk full")))
    cap = Mock()
    monkeypatch.setattr(outbox_host.outbox, "cap", cap)
    outbox_host.prune(object(), BusConfig())
    cap.assert_not_called()
    assert capsys.readouterr().err == "ledger outbox prune: disk full\n"


def test_the_outbox_limit_and_prune_interval_defaults():
    from scripts.swarm_ledger import outbox_host

    assert outbox.MAX_UNSENT == 10_000
    assert outbox_host.PRUNE_EVERY == 30.0
    assert outbox_host.INTERVAL == 1.0


def _closed_port() -> str:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return f"nats://127.0.0.1:{probe.getsockname()[1]}"


def _unsent(repository) -> int:
    with repository.connect() as connection:
        return connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]


@pytest.mark.wall_clock
@pytest.mark.parametrize("backend", ["memory", "nats"])
def test_the_outbox_stays_bounded_with_no_bus(tmp_path, monkeypatch, backend):
    import time

    from scripts.swarm_ledger import outbox_host

    repository = sqlite.SQLiteLedgerRepository(tmp_path / "ledgers.sqlite3")
    for slug in ("one", "two", "three"):
        repository.create(slug, {"title": slug, "phases": [{"title": "One"}]})
    assert _unsent(repository) == 3
    monkeypatch.setattr(outbox, "MAX_UNSENT", 1)
    monkeypatch.setenv("AGENTIHOOKS_BUS", backend)
    monkeypatch.setenv("AGENTIHOOKS_BUS_URL", _closed_port())
    with outbox_host.running(repository, interval=0.01, prune_every=0.01):
        deadline = time.monotonic() + 5
        while _unsent(repository) > 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert _unsent(repository) == 1


@pytest.mark.wall_clock
def test_a_reload_with_nats_unreachable_finishes_inside_the_stop_deadline(tmp_path, monkeypatch, capsys):
    import time

    from scripts.scheduler import process_interval
    from scripts.swarm_ledger import outbox_host

    repository = sqlite.SQLiteLedgerRepository(tmp_path / "ledgers.sqlite3")
    repository.create("ledger", {"title": "Store", "phases": [{"title": "One"}]})
    monkeypatch.setenv("AGENTIHOOKS_BUS", "nats")
    monkeypatch.setenv("AGENTIHOOKS_BUS_URL", _closed_port())
    with outbox_host.running(repository, interval=0.01):
        time.sleep(0.3)
        stopping = time.monotonic()
    assert time.monotonic() - stopping < process_interval.STOP_SECONDS
    lines = capsys.readouterr().err.splitlines()
    assert lines
    assert set(lines) == {"ledger bus relay: nats: no servers available for connection"}
    assert _unsent(repository) == 1
