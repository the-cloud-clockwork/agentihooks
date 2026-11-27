import os
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import Mock, call

import pytest

from scripts.events import factory, memory
from scripts.events.config import BusConfig
from scripts.hub import projector
from scripts.swarm_ledger.repository import outbox, sqlite
from tests.events import nats_server


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
        repository.create("ledger", {"title": "Store", "phases": [{"title": "One"}]}, "swarm")
        assert published.wait(2), "the host did not publish the committed ledger change"
    assert [subject for subject, _ in bus.events.entries] == ["swarm.ledger.phase.p1"]
    opening.assert_called_once_with(BusConfig(backend="nats"))
    close.assert_called_once_with()
    with repository.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM outbox_sent").fetchone() == (1,)


def test_memory_default_preserves_outbox_rows(tmp_path, monkeypatch):
    from scripts.swarm_ledger import outbox_host

    repository = sqlite.SQLiteLedgerRepository(tmp_path / "ledgers.sqlite3")
    repository.create("ledger", {"title": "Store", "phases": [{"title": "One"}]}, "swarm")
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


@pytest.mark.parametrize("close_error", [False, True])
def test_host_stops_the_scheduled_interval_before_closing_the_bus(monkeypatch, close_error):
    from scripts.swarm_ledger import outbox_host

    monkeypatch.setenv("AGENTIHOOKS_BUS", "nats")
    order = Mock()
    pruning, relaying, bus = order.pruning, order.relaying, order.bus
    interval = Mock(side_effect=[pruning, relaying])
    monkeypatch.setattr(outbox_host, "Interval", interval)
    opening = Mock(return_value=bus)
    monkeypatch.setattr(factory, "open_bus", opening)
    monkeypatch.setattr(outbox_host.outbox, "relay", Mock())
    monkeypatch.setattr(outbox_host.outbox, "prune", Mock())
    relaying.close.side_effect = ValueError("stop") if close_error else None
    repository = object()
    with pytest.raises(ValueError, match="stop" if close_error else "serving"):
        with outbox_host.running(repository):
            assert order.mock_calls == [call.pruning.start(), call.relaying.start()]
            callback = interval.call_args.args[0]
            callback()
            raise ValueError("serving")
    assert [entry.kwargs for entry in interval.call_args_list] == [{"every": 30.0}, {"every": 1.0}]
    assert order.mock_calls == [
        call.pruning.start(),
        call.relaying.start(),
        call.relaying.close(),
        call.bus.close(),
        call.pruning.close(),
    ]
    assert callback.__self__.bus is None
    assert callback.__self__.repository is repository
    opening.assert_called_once_with(BusConfig(backend="nats"))


def started_quietly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Let the ledger API service start with no folder lease, adoption, upkeep pass or signal handler of its own."""
    from scripts.swarm_ledger import api_service, ledger_server
    from scripts.swarm_ledger.repository import legacy

    monkeypatch.setattr(ledger_server.core, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(ledger_server.storage, "shared", lambda: None)
    monkeypatch.setattr(ledger_server.ports, "swarm", Mock(return_value=Mock()))
    monkeypatch.setattr(legacy, "adopt", Mock())
    monkeypatch.setattr(api_service.signal, "signal", Mock())
    for name in ("adopt_unrelayed", "adopt_disk_files", "upkeep"):
        monkeypatch.setattr(api_service, name, Mock())


@pytest.mark.wall_clock
def test_the_ledger_api_service_alone_drives_the_outbox_until_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm_ledger import api_service, ledger_server

    repository = sqlite.SQLiteLedgerRepository(tmp_path / "ledgers.sqlite3")
    bus = memory.open_bus(BusConfig())
    published = threading.Event()
    publish = bus.events.publish

    def observed(subject: str, event: Any) -> Any:
        result = publish(subject, event)
        published.set()
        return result

    monkeypatch.setattr(bus.events, "publish", observed)
    close = Mock(wraps=bus.close)
    monkeypatch.setattr(bus, "close", close)
    monkeypatch.setattr(factory, "open_bus", Mock(return_value=bus))
    monkeypatch.setenv("AGENTIHOOKS_BUS", "nats")
    monkeypatch.setattr(ledger_server, "stored", repository)
    started_quietly(monkeypatch, tmp_path)
    server = Mock()
    monkeypatch.setattr(ledger_server, "ThreadingHTTPServer", Mock(return_value=server))

    def serving(poll_interval: float) -> None:
        repository.create("ledger", {"title": "Store", "phases": [{"title": "One"}]}, "swarm")
        assert published.wait(3), "only the running API service should drive publication"
        close.assert_not_called()

    server.serve_forever.side_effect = serving
    api_service.serve()
    close.assert_called_once_with()
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
                repository.create("ledger", {"title": "Store", "phases": [{"title": "One"}]}, "swarm")
                deliveries = consumer.fetch(1, 3_000)
                assert [(delivery.event.entity, delivery.event.key) for delivery in deliveries] == [("phase", "p1")]
            assert len(opened) == 2
            assert all(bus._loop.closed for bus in opened)
            with repository.connect() as connection:
                assert connection.execute("SELECT COUNT(*) FROM outbox_sent").fetchone() == (1,)
        finally:
            observer.close()


def test_the_api_service_gives_the_relay_its_store_and_stops_it_before_closing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from contextlib import contextmanager

    from scripts.swarm_ledger import api_service, ledger_server, outbox_host

    lifecycle: list[str] = []

    @contextmanager
    def running(repository: object) -> Iterator[None]:
        assert repository is ledger_server.stored
        lifecycle.append("relay started")
        try:
            yield
        finally:
            lifecycle.append("relay stopped")

    def serving(poll_interval: float) -> None:
        assert poll_interval == ledger_server.POLL_S
        lifecycle.append("serving")
        raise ValueError("serving failed")

    monkeypatch.setattr(outbox_host, "running", running)
    started_quietly(monkeypatch, tmp_path)
    server = Mock()
    server.serve_forever.side_effect = serving
    server.server_close.side_effect = lambda: lifecycle.append("server closed")
    monkeypatch.setattr(ledger_server, "ThreadingHTTPServer", Mock(return_value=server))
    with pytest.raises(ValueError, match="serving failed"):
        api_service.serve()
    assert lifecycle == ["relay started", "serving", "relay stopped", "server closed"]


def test_the_ledger_server_gives_the_projector_its_store_and_stops_it_before_closing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from contextlib import contextmanager

    from scripts.swarm_ledger import ledger_server

    lifecycle: list[object] = []

    @contextmanager
    def projecting(repository: object, hub: object) -> Iterator[None]:
        assert (repository, hub) == (ledger_server.repository, ledger_server.HUB)
        lifecycle.append("projector started")
        try:
            yield
        finally:
            lifecycle.append("projector stopped")

    def serving(poll_interval: float) -> None:
        assert poll_interval == ledger_server.POLL_S
        lifecycle.append("serving")
        raise ValueError("serving failed")

    monkeypatch.setattr(projector, "running", projecting)
    monkeypatch.setattr(ledger_server, "PIDFILE", tmp_path / "server.pid")
    monkeypatch.setattr(ledger_server.core, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(ledger_server.storage, "shared", lambda: None)
    monkeypatch.setattr(ledger_server.threading, "Thread", Mock())
    monkeypatch.setattr(ledger_server, "metrics_journal", Mock())
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
        "projector started",
        "serving",
        ("drained", ledger_server.DRAIN_S),
        "projector stopped",
        "server closed",
    ]


def patched_serve(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, server: Mock) -> Path:
    from scripts.swarm_ledger import ledger_server

    pidfile = tmp_path / "server.pid"
    monkeypatch.setattr(ledger_server, "PIDFILE", pidfile)
    monkeypatch.setattr(ledger_server.core, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(ledger_server.storage, "shared", lambda: None)
    monkeypatch.setattr(ledger_server.threading, "Thread", Mock())
    monkeypatch.setattr(ledger_server, "metrics_journal", Mock())
    monkeypatch.setattr(ledger_server.server_code, "record", Mock())
    monkeypatch.setattr(ledger_server.server_lifetime, "watch", Mock())
    monkeypatch.setattr(ledger_server, "ThreadingHTTPServer", Mock(return_value=server))
    return pidfile


@pytest.mark.feature("ledger-server-pid-file-after-projector")
def test_the_ledger_server_writes_its_pid_file_only_once_the_projector_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from scripts.swarm_ledger import ledger_server

    seen: list[tuple[str, str | None]] = []
    server = Mock()
    server.drained.return_value = True
    pidfile = patched_serve(monkeypatch, tmp_path, server)

    def written() -> str | None:
        return pidfile.read_text() if pidfile.exists() else None

    @contextmanager
    def projecting(repository: object, hub: object) -> Iterator[None]:
        seen.append(("projector started", written()))
        yield

    def serving(poll_interval: float) -> None:
        seen.append(("serving", written()))
        raise ValueError("serving failed")

    server.serve_forever.side_effect = serving
    monkeypatch.setattr(projector, "running", projecting)
    with pytest.raises(ValueError, match="serving failed"):
        ledger_server.serve()
    assert seen == [("projector started", None), ("serving", str(os.getpid()))]


@pytest.mark.feature("ledger-server-pid-file-after-projector")
def test_a_ledger_server_whose_projector_fails_to_start_writes_no_pid_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from scripts.swarm_ledger import ledger_server

    server = Mock()
    pidfile = patched_serve(monkeypatch, tmp_path, server)
    monkeypatch.setattr(projector, "running", Mock(side_effect=RuntimeError("projector failed")))
    with pytest.raises(RuntimeError, match="projector failed"):
        ledger_server.serve()
    assert not pidfile.exists()
    server.serve_forever.assert_not_called()


def test_a_relay_that_fails_to_start_releases_the_lease_without_serving(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from scripts.swarm_ledger import api_service, ledger_server, outbox_host

    def broken(repository: object) -> None:
        raise RuntimeError("relay refused")

    monkeypatch.setattr(outbox_host, "running", broken)
    started_quietly(monkeypatch, tmp_path)
    server = Mock()
    monkeypatch.setattr(ledger_server, "ThreadingHTTPServer", Mock(return_value=server))
    with pytest.raises(RuntimeError, match="relay refused"):
        api_service.serve()
    server.serve_forever.assert_not_called()
    server.server_close.assert_called_once_with()
    ledger_server.ports.swarm.return_value.ledger_writer.WriterLease.return_value.release.assert_called_once_with()


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

    repository.create("old", {"title": "Old", "phases": [{"title": "One"}]}, "swarm")
    stale = ids()
    outbox.relay(repository, bus.events, clock=lambda: 1_000)
    repository.create("recent", {"title": "Recent", "phases": [{"title": "One"}]}, "swarm")
    recent = ids() - stale
    outbox.relay(repository, bus.events, clock=lambda: outbox.now_ms() - config.dedupe_window_ms // 2)
    repository.create("unsent", {"title": "Unsent", "phases": [{"title": "One"}]}, "swarm")
    unsent = ids() - stale - recent
    assert [len(stale), len(recent), len(unsent)] == [1, 1, 1]
    opening = Mock()
    monkeypatch.setattr(factory, "open_bus", opening)
    outbox_host.prune(repository, config)
    assert ids() == recent | unsent
    assert marks() == recent
    opening.assert_not_called()


@pytest.mark.parametrize("dropped", [0, 3])
def test_the_host_prune_caps_unsent_rows_at_the_outbox_limit_and_reports_a_drop(monkeypatch, capsys, dropped):
    from scripts.swarm_ledger import outbox_host

    prune, cap = Mock(return_value=0), Mock(return_value=dropped)
    monkeypatch.setattr(outbox_host.outbox, "prune", prune)
    monkeypatch.setattr(outbox_host.outbox, "cap", cap)
    monkeypatch.setattr(outbox_host.outbox, "MAX_UNSENT", 7)
    repository = object()
    outbox_host.prune(repository, BusConfig(dedupe_window_ms=9_000))
    prune.assert_called_once_with(repository, 9_000)
    cap.assert_called_once_with(repository, 7)
    assert capsys.readouterr().err == ("ledger outbox prune: dropped 3 unsent rows past 7\n" if dropped else "")


def test_a_failed_prune_logs_one_line_and_skips_the_cap(monkeypatch, capsys):
    from scripts.swarm_ledger import outbox_host

    monkeypatch.setattr(outbox_host.outbox, "prune", Mock(side_effect=OSError("disk full")))
    cap = Mock()
    monkeypatch.setattr(outbox_host.outbox, "cap", cap)
    outbox_host.prune(object(), BusConfig())
    cap.assert_not_called()
    assert capsys.readouterr().err == "ledger outbox prune: disk full\n"


def test_scheduled_runs_its_callback_on_its_own_interval_until_the_block_ends():
    from scripts.swarm_ledger import outbox_host

    ran = threading.Event()
    with outbox_host.scheduled(ran.set, 0.001):
        assert ran.wait(1)


def test_running_prunes_the_given_repository_with_the_bus_config_on_the_memory_bus(monkeypatch):
    from scripts.swarm_ledger import outbox_host

    repository = object()
    calls = []
    pruned = threading.Event()

    def recording(*arguments: object) -> None:
        calls.append(arguments)
        pruned.set()

    monkeypatch.setenv("AGENTIHOOKS_BUS", "memory")
    monkeypatch.setattr(outbox_host, "prune", recording)
    with outbox_host.running(repository, prune_every=0.001):
        assert pruned.wait(1)
    assert calls[0][0] is repository
    assert isinstance(calls[0][1], BusConfig)


def test_the_outbox_limit_and_prune_interval_defaults():
    from scripts.swarm_ledger import outbox_host

    assert outbox.MAX_UNSENT == 10_000
    assert outbox_host.PRUNE_EVERY == 30.0
    assert outbox_host.INTERVAL == 1.0


def _unsent(repository: sqlite.SQLiteLedgerRepository) -> int:
    with repository.connect() as connection:
        return connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]


@pytest.mark.wall_clock
@pytest.mark.parametrize("backend", ["memory", "nats"])
def test_the_outbox_stays_bounded_with_no_bus(tmp_path, monkeypatch, backend):
    import time

    from scripts.swarm_ledger import outbox_host

    repository = sqlite.SQLiteLedgerRepository(tmp_path / "ledgers.sqlite3")
    for slug in ("one", "two", "three"):
        repository.create(slug, {"title": slug, "phases": [{"title": "One"}]}, "swarm")
    assert _unsent(repository) == 3
    monkeypatch.setattr(outbox, "MAX_UNSENT", 1)
    monkeypatch.setenv("AGENTIHOOKS_BUS", backend)
    monkeypatch.setenv("AGENTIHOOKS_BUS_URL", nats_server.closed_url())
    with outbox_host.running(repository, interval=0.01, prune_every=0.01):
        deadline = time.monotonic() + 5
        while _unsent(repository) > 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert _unsent(repository) == 1


def _reload_with_nats_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[sqlite.SQLiteLedgerRepository, float]:
    import time

    from scripts.swarm_ledger import outbox_host

    repository = sqlite.SQLiteLedgerRepository(tmp_path / "ledgers.sqlite3")
    repository.create("ledger", {"title": "Store", "phases": [{"title": "One"}]}, "swarm")
    monkeypatch.setenv("AGENTIHOOKS_BUS", "nats")
    monkeypatch.setenv("AGENTIHOOKS_BUS_URL", nats_server.closed_url())
    with outbox_host.running(repository, interval=0.01):
        time.sleep(0.3)
        stopping = time.monotonic()
    return repository, time.monotonic() - stopping


@pytest.mark.wall_clock
def test_a_reload_with_nats_unreachable_finishes_inside_the_stop_deadline(tmp_path, monkeypatch):
    from scripts.scheduler import process_interval

    _, stopped_in = _reload_with_nats_unreachable(tmp_path, monkeypatch)
    assert stopped_in < process_interval.STOP_SECONDS


def test_failed_opens_with_nats_unreachable_log_only_the_relay_line_and_keep_the_row(tmp_path, monkeypatch, capsys):
    repository, _ = _reload_with_nats_unreachable(tmp_path, monkeypatch)
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) >= 1
    shape = re.compile(
        r"ledger bus relay: nats: no server at nats://127\.0\.0\.1:\d+ answered the first connect within 5 retries"
    )
    assert [line for line in lines if not shape.fullmatch(line)] == []
    assert _unsent(repository) == 1
