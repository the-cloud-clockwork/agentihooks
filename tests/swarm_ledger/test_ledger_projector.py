import threading
from unittest.mock import Mock

import pytest

from scripts.events import envelope, factory, memory
from scripts.events.config import BusConfig
from scripts.swarm_ledger import projector
from scripts.swarm_ledger.events.hub import Hub
from scripts.swarm_ledger.events.publishing import Publishing
from scripts.swarm_ledger.repository import outbox, sqlite


def event(slug, kind="task.changed", key="t1"):
    entity = kind.split(".")[0]
    return envelope.create(type=kind, swarm=slug, entity=entity, key=key, version=1, author="ledger", data={})


def watching(*slugs):
    hub = Hub()
    for slug in slugs:
        hub.open(slug, lambda: {})
    return hub


def test_identity_is_lowercase_letters_digits_and_dashes():
    assert projector.identity("Ledger_Pod.0", 8765) == "ledger-pod-0-8765"


def test_reactor_follows_every_swarm_subject_under_a_durable_per_process():
    reactor = projector.Projector(Mock(), Hub(), "pod-8765").reactor()
    assert (reactor.name, reactor.subject, reactor.singleton) == ("projector-pod-8765", "swarm.>", False)


def test_a_ledger_event_refreshes_only_a_watched_ledger():
    repository = Mock()
    project = projector.Projector(repository, watching("seen"), "pod")
    project.handle(event("seen"))
    project.handle(event("seen", "phase.removed", "p1"))
    project.handle(event("other"))
    project.handle(event("seen", "execution.exited"))
    assert [call.args for call in repository.current.call_args_list] == [("seen",), ("seen",)]


def test_a_failed_refresh_naks_with_the_reactor_retry_delay_and_a_success_acks():
    repository = Mock()
    repository.current.side_effect = [OSError("database down"), None]
    failed, handled = Mock(event=event("seen"), attempt=2), Mock(event=event("seen"), attempt=1)
    consumer = Mock()
    consumer.fetch.return_value = [failed, handled]
    project = projector.Projector(repository, watching("seen"), "pod")
    assert project.pass_once(consumer, (1_000, 5_000)) == ["failed", "handled"]
    consumer.fetch.assert_called_once_with(projector.BATCH, projector.FETCH_TIMEOUT_MS)
    failed.nak.assert_called_once_with(5_000)
    failed.ack.assert_not_called()
    handled.ack.assert_called_once_with()
    handled.nak.assert_not_called()


def stored_ledger(tmp_path):
    inner = sqlite.SQLiteLedgerRepository(tmp_path / "ledgers.sqlite3")
    inner.create("seen", {"title": "Seen", "phases": [{"title": "One"}]})
    inner.create("other", {"title": "Other", "phases": [{"title": "One"}]})
    return inner


def test_a_bus_delivery_of_another_hosts_write_reaches_the_watched_page_only(tmp_path):
    inner = stored_ledger(tmp_path)
    hub = Hub()
    repository = Publishing(inner, lambda slug, state: hub.publish_snapshot(slug, "ledger", state))
    hub.open("seen", lambda: {"ledger": repository.get_document("seen")})
    repository.drain(5)
    before = hub.resource("seen", "ledger")["_meta"]["rev"]
    bus = memory.open_bus(BusConfig())
    outbox.relay(inner, bus.events)
    consumer = bus.events.consumer("projector-test", "swarm.>")
    for delivery in consumer.fetch(100, 0):
        delivery.ack()
    inner.apply_ops("seen", changes=[{"path": "phases/p1/done", "value": True, "base": False}])
    inner.apply_ops("other", changes=[{"path": "phases/p1/done", "value": True, "base": False}])
    outbox.relay(inner, bus.events)
    project = projector.Projector(repository, hub, "pod")
    assert project.pass_once(consumer, ()) == ["handled", "handled"]
    repository.drain(5)
    after = hub.resource("seen", "ledger")
    assert after["_meta"]["rev"] > before
    assert after["phases"][0]["done"] is True
    assert not hub.has("other")


def test_memory_bus_runs_no_projector(monkeypatch):
    opening = Mock()
    monkeypatch.setattr(factory, "open_bus", opening)
    with projector.running(Mock(), Hub(), 8765, environ={}) as running:
        assert running is None
    opening.assert_not_called()


@pytest.mark.wall_clock
def test_running_projector_refreshes_on_a_published_event_and_closes_its_bus_on_exit(monkeypatch):
    bus = memory.open_bus(BusConfig())
    close = Mock(wraps=bus.close)
    monkeypatch.setattr(bus, "close", close)
    opening = Mock(return_value=bus)
    monkeypatch.setattr(factory, "open_bus", opening)
    refreshed = threading.Event()
    repository = Mock()
    repository.current.side_effect = lambda slug: refreshed.set()
    environ = {"AGENTIHOOKS_BUS": "nats", "HOSTNAME": "Pod.A"}
    with projector.running(repository, watching("seen"), 8765, environ=environ) as running:
        assert running.name == "pod-a-8765"
        bus.events.publish("swarm.seen.task.t1", event("seen"))
        assert refreshed.wait(5), "the projector did not refresh the watched ledger"
        worker = running.worker
    assert not worker.is_alive()
    opening.assert_called_once_with(BusConfig(backend="nats"))
    close.assert_called_once_with()
    repository.current.assert_called_once_with("seen")


@pytest.mark.wall_clock
def test_a_failed_bus_open_is_logged_and_retried_until_stopped(monkeypatch, capsys):
    opened = threading.Event()

    def refused(config):
        opened.set()
        raise OSError("bus unreachable")

    monkeypatch.setattr(factory, "open_bus", refused)
    monkeypatch.setattr(projector, "REOPEN_S", 0.01)
    with projector.running(Mock(), Hub(), 8765, environ={"AGENTIHOOKS_BUS": "nats", "HOSTNAME": "pod"}):
        assert opened.wait(5)
    assert "ledger projector: bus unreachable" in capsys.readouterr().err
