import threading
import time
from dataclasses import replace
from unittest.mock import Mock

import pytest

from scripts.events import envelope, factory, memory, nats
from scripts.events.config import BusConfig, QueuePolicy
from scripts.hub import projector, registry
from scripts.swarm_ledger.events.hub import Hub
from scripts.swarm_ledger.events.publishing import Publishing
from scripts.swarm_ledger.repository import outbox, sqlite

NATS = BusConfig(backend="nats")


def event(slug, kind="task.changed", key="t1"):
    entity = kind.split(".")[0]
    return envelope.create(type=kind, swarm=slug, entity=entity, key=key, version=1, author="ledger", data={})


def watching(*slugs):
    hub = Hub()
    for slug in slugs:
        hub.open(slug, lambda: {})
    return hub


def make(repository=None, hub=None, name="pod", clock=lambda: 0.0):
    return projector.Projector(repository or Mock(), hub or Hub(), name, NATS, clock)


def durables(bus) -> list[str]:
    return sorted(info.name for info in bus._loop.run(bus._js.consumers_info(nats.EVENT_STREAM)))


@pytest.mark.parametrize(
    ("host", "name"), [("Ledger_Pod.0", "ledger-pod-0-8765"), ("_pod.", "pod-8765"), ("..", "8765")]
)
def test_identity_is_a_valid_reactor_name(host, name):
    assert projector.identity(host, 8765) == name
    assert make(name=name).reactor.name == f"projector-{name}"


def test_reactor_follows_every_swarm_subject_under_a_durable_per_process():
    reactor = make(name="pod-8765").reactor
    assert (reactor.name, reactor.subject, reactor.singleton) == ("projector-pod-8765", "swarm.>", False)


def test_reactor_policy_carries_a_ten_minute_inactive_threshold_with_the_reactor_deliveries():
    assert make().reactor.policy == QueuePolicy(max_deliver=50, inactive_ms=600_000)


def test_a_rollout_leaves_only_the_live_projector_durable_on_the_bus(monkeypatch, tmp_path):
    from tests.events import nats_server

    monkeypatch.setattr(projector, "POLICY", replace(projector.POLICY, inactive_ms=500))
    with nats_server.running(tmp_path / "jetstream") as url:
        config = BusConfig(backend="nats", url=url)
        old = projector.Projector(Mock(), Hub(), "pod-a-8765", config)
        old.pull()
        assert durables(old.bus) == ["hub-projector-pod-a-8765"]
        old.close()
        live = projector.Projector(Mock(), Hub(), "pod-b-8765", config)
        try:
            live.pull()
            deadline = time.monotonic() + 15
            while durables(live.bus) != ["hub-projector-pod-b-8765"] and time.monotonic() < deadline:
                live.pull()
            assert durables(live.bus) == ["hub-projector-pod-b-8765"]
        finally:
            live.close()


def test_a_ledger_event_refreshes_only_a_watched_ledger():
    repository = Mock()
    project = make(repository, watching("seen"))
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
    project = make(repository, watching("seen"))
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
    hub.open("other", lambda: {"ledger": repository.get_document("other")})
    hub.close("other")
    repository.drain(5)
    before = hub.resource("seen", "ledger")["_meta"]["rev"]
    other = hub.resource("other", "ledger")["_meta"]["rev"]
    bus = memory.open_bus(BusConfig())
    outbox.relay(inner, bus.events)
    consumer = bus.events.consumer("projector-test", "swarm.>")
    for delivery in consumer.fetch(100, 0):
        delivery.ack()
    inner.apply_ops("seen", changes=[{"path": "phases/p1/done", "value": True, "base": False}])
    inner.apply_ops("other", changes=[{"path": "phases/p1/done", "value": True, "base": False}])
    outbox.relay(inner, bus.events)
    assert make(repository, hub).pass_once(consumer, ()) == ["handled", "handled"]
    repository.drain(5)
    after = hub.resource("seen", "ledger")
    assert after["_meta"]["rev"] > before
    assert after["phases"][0]["done"] is True
    assert hub.resource("other", "ledger")["_meta"]["rev"] == other


def test_memory_bus_runs_no_projector(monkeypatch):
    opening = Mock()
    monkeypatch.setattr(factory, "open_bus", opening)
    with projector.running(Mock(), Hub(), environ={}) as running:
        assert running is None
    opening.assert_not_called()


def test_running_projector_refreshes_on_a_published_event_and_closes_its_bus_on_exit(monkeypatch, tmp_path):
    bus = memory.open_bus(BusConfig())
    close = Mock(wraps=bus.close)
    monkeypatch.setattr(bus, "close", close)
    opening = Mock(return_value=bus)
    monkeypatch.setattr(factory, "open_bus", opening)
    refreshed = threading.Event()
    repository = Mock()
    repository.current.side_effect = lambda slug: refreshed.set()
    environ = {"AGENTIHOOKS_BUS": "nats", "HOSTNAME": "Pod.A", "LEDGER_DIR": str(tmp_path), "LEDGER_PORT": "18472"}
    with projector.running(repository, watching("seen"), environ=environ) as running:
        assert running.name == "pod-a-18472"
        bus.events.publish("swarm.seen.task.t1", event("seen"))
        assert refreshed.wait(5), "the projector did not refresh the watched ledger"
    assert running.bus is None
    opening.assert_called_once_with(NATS)
    close.assert_called_once_with()
    repository.current.assert_called_once_with("seen")


def test_pull_opens_its_durable_once_and_naks_a_failed_refresh_with_the_reactor_delays(monkeypatch):
    bus = Mock()
    failed = Mock(event=event("seen"), attempt=1)
    bus.events.consumer.return_value.fetch.return_value = [failed]
    opening = Mock(return_value=bus)
    monkeypatch.setattr(factory, "open_bus", opening)
    repository = Mock()
    repository.current.side_effect = OSError("database down")
    project = make(repository, watching("seen"))
    project.pull()
    project.pull()
    opening.assert_called_once_with(NATS)
    bus.events.consumer.assert_called_once_with("hub-projector-pod", "swarm.>", projector.POLICY)
    assert failed.nak.call_args_list == [((registry.RETRY_MS[0],),)] * 2
    bus.close.assert_not_called()


def test_a_refused_bus_is_logged_and_reopened_only_after_the_reopen_delay(monkeypatch, capsys):
    opening = Mock(side_effect=OSError("bus unreachable"))
    monkeypatch.setattr(factory, "open_bus", opening)
    now = [100.0]
    project = make(clock=lambda: now[0])
    project.pull()
    assert capsys.readouterr().err == "ledger projector: bus unreachable\n"
    assert project.reopen_at == 100.0 + projector.REOPEN_S
    now[0] = 100.0 + projector.REOPEN_S - 0.01
    project.pull()
    assert opening.call_count == 1
    now[0] = 100.0 + projector.REOPEN_S
    project.pull()
    assert opening.call_count == 2


def test_a_failed_fetch_closes_the_bus_so_the_next_pull_reopens_it(monkeypatch, capsys):
    bus = Mock()
    bus.events.consumer.return_value.fetch.side_effect = OSError("connection lost")
    monkeypatch.setattr(factory, "open_bus", Mock(return_value=bus))
    project = make()
    project.pull()
    assert capsys.readouterr().err == "ledger projector: connection lost\n"
    bus.close.assert_called_once_with()
    assert (project.bus, project.consumer) == (None, None)
    project.close()
    bus.close.assert_called_once_with()
