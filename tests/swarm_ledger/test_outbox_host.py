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
    assert [subject for subject, _ in bus.events.entries] == ["ledger.ledger.phase.created"]
    opening.assert_called_once_with(BusConfig(backend="nats"))
    close.assert_called_once_with()
    with repository.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM outbox_sent").fetchone() == (1,)
