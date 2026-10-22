import asyncio
import threading

import pytest

from scripts.events import factory, nats
from scripts.events.bus import Bus, Consumer, Delivery, EventLog, StateStore, WorkQueue
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
    threads = {threading.current_thread() for _ in store.watch("k")}
    assert threads == {threading.current_thread()}
    assert bus._loop.thread not in threads


def test_close_stops_the_loop_thread_and_refuses_later_calls(url):
    opened = nats.open_bus(BusConfig(backend="nats", url=url))
    opened.close()
    assert not opened._loop.thread.is_alive()
    with pytest.raises(RuntimeError, match="^bus is closed$"):
        opened.events.publish("swarm.s.task", event())
