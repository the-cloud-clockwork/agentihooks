import asyncio
import threading
import time
from itertools import islice
from types import SimpleNamespace
from unittest import mock

import pytest
from nats.js import api
from nats.js.errors import APIError

from scripts.events import factory, nats
from scripts.events.bus import Bus, Consumer, Delivery, EventLog, StateConflict, StateStore, WorkQueue
from scripts.events.config import BusConfig, QueuePolicy
from scripts.events.envelope import Envelope

from . import nats_server

pytestmark = pytest.mark.unit

POLICY = QueuePolicy(max_deliver=3, ack_wait_ms=1_000, backoff_ms=(200, 400, 800))
LOG_POLICY = QueuePolicy(max_deliver=3, ack_wait_ms=1_000)


def event(event_id: str = "e1") -> Envelope:
    return Envelope(
        id=event_id, type="task.claimed", swarm="s", entity="task", key="t1", version=1, time=0, author="a", data={}
    )


@pytest.fixture
def url(tmp_path):
    with nats_server.running(tmp_path / "jetstream") as running:
        yield running


@pytest.fixture
def bus(url):
    opened = nats.open_bus(BusConfig(backend="nats", url=url, queue=LOG_POLICY))
    yield opened
    opened.close()


def _consumer_config(bus, stream: str, durable: str):
    return bus._loop.run(bus._js.consumer_info(stream, durable)).config


def test_the_loop_runs_every_coroutine_on_one_private_thread():
    loop = nats._Loop()

    async def where():
        return threading.current_thread()

    first, second = loop.run(where()), loop.run(where())
    loop.close()
    assert first is second is loop.thread
    assert first is not threading.current_thread()
    assert first.name == "agentihooks-nats-bus"
    assert first.daemon is True
    assert not first.is_alive()


def test_the_loop_raises_the_coroutine_error_in_the_caller():
    loop = nats._Loop()

    async def fail():
        raise LookupError("boom")

    with pytest.raises(LookupError, match="^boom$"):
        loop.run(fail())
    loop.close()


def test_the_loop_refuses_work_once_closed():
    loop = nats._Loop()
    loop.close()
    coro = asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="^bus is closed$"):
        loop.run(coro)
    loop.close()


def test_the_factory_opens_the_nats_backend(url):
    opened = factory.open_bus(BusConfig(backend="nats", url=url))
    try:
        assert isinstance(opened, nats.NatsBus)
        assert isinstance(opened, Bus)
    finally:
        opened.close()


def test_open_bus_defaults_to_the_local_server_url(monkeypatch):
    seen = []
    monkeypatch.setattr(nats.NatsBus, "__init__", lambda self, config, url: seen.append(url))
    nats.open_bus(BusConfig(backend="nats"))
    assert seen == ["nats://127.0.0.1:4222"]


def test_every_part_satisfies_its_bus_protocol(bus):
    assert isinstance(bus.events, EventLog)
    assert isinstance(bus.events.consumer("d", "swarm.>"), Consumer)
    queue = bus.queue("work", POLICY)
    assert isinstance(queue, WorkQueue)
    queue.offer(event())
    assert isinstance(queue.take(500), Delivery)
    assert isinstance(bus.state("tasks"), StateStore)


def test_the_event_log_and_queue_create_only_pull_consumers(bus):
    bus.events.consumer("watcher", "swarm.>")
    bus.queue("work", POLICY)
    log = _consumer_config(bus, nats.EVENT_STREAM, "watcher")
    work = _consumer_config(bus, nats.queue_stream("work"), "work")
    assert (log.deliver_subject, work.deliver_subject) == (None, None)
    assert (log.filter_subject, work.filter_subject) == ("swarm.>", "agentihooks.queue.work")


def test_a_consumer_carries_the_queue_policy(bus):
    bus.events.consumer("watcher", "swarm.>")
    bus.queue("work", POLICY)
    log = _consumer_config(bus, nats.EVENT_STREAM, "watcher")
    work = _consumer_config(bus, nats.queue_stream("work"), "work")
    assert (log.max_deliver, log.ack_wait, log.backoff) == (3, 1.0, None)
    assert (work.max_deliver, work.backoff) == (3, [0.2, 0.4])


def test_a_durable_consumer_refuses_another_filter(bus):
    bus.events.consumer("watcher", "swarm.a.>")
    with pytest.raises(ValueError, match=r"^durable consumer 'watcher' filters 'swarm.a.>', not 'swarm.b.>'$"):
        bus.events.consumer("watcher", "swarm.b.>")


def test_a_work_queue_refuses_another_policy(bus):
    bus.queue("work", POLICY)
    assert bus.queue("work", POLICY) is bus.queue("work", POLICY)
    with pytest.raises(ValueError, match=r"^work queue 'work' already has another policy$"):
        bus.queue("work", QueuePolicy())


def test_a_delivery_reports_its_event_and_attempt(bus):
    queue = bus.queue("work", POLICY)
    queue.offer(event("e7"))
    delivery = queue.take(500)
    assert (delivery.event, delivery.attempt) == (event("e7"), 1)
    delivery.term()
    assert queue.take(1_000) is None


def test_the_facade_serves_calls_from_many_caller_threads(bus):
    queue = bus.queue("work", POLICY)
    offered = []
    threads = [threading.Thread(target=lambda i=i: offered.append(queue.offer(event(f"e{i}")))) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert offered == [True] * 8
    taken = {queue.take(500).event.id for _ in range(8)}
    assert taken == {f"e{i}" for i in range(8)}


def test_watch_runs_its_body_on_the_caller_thread(bus):
    store = bus.state("tasks")
    store.create("k", b"v1")
    threads = [threading.current_thread() for _ in islice(store.watch("k"), 2)]
    assert threads == [threading.current_thread()]
    assert bus._loop.thread not in threads


def test_close_stops_the_loop_thread_and_refuses_later_calls(url):
    opened = nats.open_bus(BusConfig(backend="nats", url=url))
    opened.close()
    assert not opened._loop.thread.is_alive()
    with pytest.raises(RuntimeError, match="^bus is closed$"):
        opened.events.publish("swarm.s.task", event())


def test_a_new_loop_starts_open():
    loop = nats._Loop()
    assert loop.closed is False
    loop.close()
    assert loop.closed is True


class _Idle:
    def __init__(self, *args, **kwargs):
        self.started = False

    def start(self):
        self.started = True

    def join(self, timeout):
        pass

    def is_alive(self):
        return False


class _Unset:
    timeouts = []

    def set(self):
        pass

    def wait(self, timeout):
        self.timeouts.append(timeout)
        return False


class _Stuck:
    def __init__(self):
        self.timeouts = []

    def join(self, timeout):
        self.timeouts.append(timeout)

    def is_alive(self):
        return True


def test_a_loop_that_never_runs_is_refused_at_its_start_bound(monkeypatch):
    monkeypatch.setattr(_Unset, "timeouts", [])
    monkeypatch.setattr(nats.threading, "Thread", _Idle)
    monkeypatch.setattr(nats.threading, "Event", _Unset)
    with pytest.raises(RuntimeError, match="^bus loop did not start$"):
        nats._Loop()
    assert _Unset.timeouts == [nats.LOOP_START_S]


class _Late(_Unset):
    def wait(self, timeout):
        time.sleep(0.2)
        return super().wait(timeout)


def test_a_running_loop_that_misses_its_start_bound_is_stopped_and_closed(monkeypatch):
    monkeypatch.setattr(_Unset, "timeouts", [])
    threads = []

    def record(*args, **kwargs):
        threads.append(threading.Thread(*args, **kwargs))
        return threads[-1]

    monkeypatch.setattr(nats, "threading", SimpleNamespace(Thread=record, Event=_Late))
    with pytest.raises(RuntimeError, match="^bus loop did not start$"):
        nats._Loop()
    threads[0].join(5)
    assert not threads[0].is_alive()


def test_a_loop_that_never_stops_is_refused_at_its_stop_bound():
    loop = nats._Loop()
    thread, stuck = loop.thread, _Stuck()
    loop.thread = stuck
    with pytest.raises(RuntimeError, match="^bus loop did not stop$"):
        loop.close()
    assert stuck.timeouts == [nats.LOOP_STOP_S]
    thread.join(5)
    assert not thread.is_alive()
    loop._loop.close()


def test_the_consumer_config_asks_for_explicit_acks():
    config = nats._consumer_config("d", "swarm.>", POLICY)
    assert (config.durable_name, config.filter_subject) == ("d", "swarm.>")
    assert config.ack_policy == api.AckPolicy.EXPLICIT
    assert (config.max_deliver, config.ack_wait, config.backoff) == (3, 1.0, [0.2, 0.4])


def test_a_work_queue_stream_keeps_work_queue_retention(bus):
    bus.queue("work", POLICY)
    info = bus._loop.run(bus._js.stream_info(nats.queue_stream("work")))
    assert info.config.retention == api.RetentionPolicy.WORK_QUEUE


def test_the_bus_connects_without_reconnecting(url, monkeypatch):
    connect = mock.Mock(wraps=nats.nats_client.connect)
    monkeypatch.setattr(nats.nats_client, "connect", connect)
    opened = nats.open_bus(BusConfig(backend="nats", url=url))
    opened.close()
    connect.assert_called_once_with(url, allow_reconnect=False)


def test_close_closes_the_server_connection(url):
    opened = nats.open_bus(BusConfig(backend="nats", url=url))
    connection = opened._nc
    opened.close()
    assert connection.is_closed


def test_close_stops_the_loop_when_the_connection_close_fails(url, monkeypatch):
    opened = nats.open_bus(BusConfig(backend="nats", url=url))
    connection = opened._nc

    async def refuse():
        raise OSError("connection close refused")

    monkeypatch.setattr(connection, "close", refuse)
    with pytest.raises(OSError, match="^connection close refused$"):
        opened.close()
    assert opened._loop.closed is True
    assert not opened._loop.thread.is_alive()


def test_the_event_log_refuses_a_subject_another_stream_owns(bus):
    bus.queue("work", POLICY)
    with pytest.raises(APIError):
        bus.events.publish("agentihooks.queue.work", event())


def test_an_offer_refuses_a_subject_another_stream_owns(bus):
    queue = bus.queue("work", POLICY)
    bus._loop.run(bus._js.delete_stream(nats.queue_stream("work")))
    bus._loop.run(bus._js.add_stream(name="OTHER", subjects=["agentihooks.queue.work"]))
    with pytest.raises(APIError):
        queue.offer(event())


def test_a_consumer_fetches_a_whole_batch(bus):
    consumer = bus.events.consumer("watcher", "swarm.>")
    for name in ("e1", "e2", "e3"):
        bus.events.publish("swarm.s.task", event(name))
    assert [d.event.id for d in consumer.fetch(2, 1_000)] == ["e1", "e2"]


def test_a_nak_without_a_delay_redelivers_at_once():
    seen = []

    async def nak(delay):
        seen.append(delay)

    msg = SimpleNamespace(data=event().encode(), metadata=SimpleNamespace(num_delivered=1), nak=nak)
    loop = nats._Loop()
    try:
        nats.NatsDelivery(loop, msg).nak()
    finally:
        loop.close()
    assert seen == [0]


def test_the_state_store_is_shared_per_bucket(bus):
    assert bus.state("tasks") is bus.state("tasks")
    assert bus.state("tasks") is not bus.state("hives")


def test_state_conflicts_name_the_key_and_revision(bus):
    store = bus.state("tasks")
    revision = store.create("k", b"v1")
    with pytest.raises(StateConflict, match=r"^state key 'k' exists$"):
        store.create("k", b"v2")
    with pytest.raises(StateConflict, match=rf"^state key 'k' is not at revision {revision + 1}$"):
        store.update("k", b"v2", revision + 1)


def test_watch_yields_an_empty_value_and_starts_at_the_first_revision(bus):
    store = bus.state("tasks")
    store.create("k", b"")
    store.update("k", b"v2", 1)
    assert [(e.value, e.revision) for e in islice(store.watch("k"), 3)] == [(b"", 1), (b"v2", 2)]
