import asyncio
import inspect
import logging
from unittest.mock import AsyncMock, Mock

import pytest

from scripts.events import memory, nats, scope
from scripts.events.config import BusConfig, QueuePolicy
from scripts.events.envelope import Envelope

pytestmark = pytest.mark.unit


def _offer() -> Envelope:
    return Envelope("e1", "task.ready", "s", "task", "t1", 1, 0, "a", {})


async def _fail(reason: str) -> None:
    raise LookupError(reason)


def test_a_scoped_queue_forwards_a_release_to_its_queue() -> None:
    queue, delivery = Mock(), Mock()
    scope.ScopedQueue(queue, "rig", QueuePolicy()).release(delivery, 5_000)
    queue.release.assert_called_once_with(delivery, 5_000)


def test_a_delivery_handed_back_twice_comes_back_once_at_its_own_attempt() -> None:
    now = [1_000]
    policy = QueuePolicy(max_deliver=3, ack_wait_ms=50)
    bus = memory.open_bus(BusConfig(dedupe_window_ms=100, queue=policy), clock=lambda: now[0])
    try:
        queue = bus.queue("release", policy)
        queue.offer(_offer())
        first = queue.take(0)
        assert first is not None and first.attempt == 1
        queue.release(first, 10)
        queue.release(first, 10)
        now[0] += 10
        again = queue.take(0)
        assert again is not None and (again.event.id, again.attempt) == ("e1", 1)
        assert queue.take(0) is None
    finally:
        bus.close()


def test_a_nats_queue_hands_a_release_to_its_delivery_with_its_own_stream() -> None:
    queue = object.__new__(nats.NatsWorkQueue)
    queue._js, queue._subject, queue._stream = Mock(), "swarm.rig.ready.eng", "queue"
    delivery = Mock()
    queue.release(delivery, 5_000)
    delivery.release.assert_called_once_with(queue._js, "swarm.rig.ready.eng", "queue", 5_000)


def test_a_nats_delivery_queues_its_copy_on_the_given_stream_then_acks_the_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish = AsyncMock()
    monkeypatch.setattr(nats.nats_payload, "publish", publish)
    metadata = Mock(num_delivered=4, sequence=Mock(stream=7))
    msg = Mock(data=_offer().encode(), headers=None, metadata=metadata, ack=AsyncMock())
    loop, js = Mock(), Mock()
    nats.NatsDelivery(loop, msg).release(js, "swarm.rig.ready.eng", "queue", 0)
    asyncio.run(loop.submit.call_args.args[0])
    headers = {nats.MSG_ID: "release:7", nats.ATTEMPT_BASE: "3"}
    publish.assert_awaited_once_with(js, "swarm.rig.ready.eng", _offer().encode(), stream="queue", headers=headers)
    msg.ack.assert_awaited_once_with()


def test_a_submitted_call_that_fails_is_logged_with_its_error(caplog: pytest.LogCaptureFixture) -> None:
    loop = nats._Loop()
    try:
        with caplog.at_level(logging.ERROR, logger=nats.__name__):
            loop.submit(_fail("copy refused"))
            loop.run(asyncio.sleep(0))
    finally:
        loop.close()
    logged = [(record.levelno, record.getMessage()) for record in caplog.records if record.name == nats.__name__]
    assert logged == [(logging.ERROR, "bus call failed: copy refused")]


def test_a_closed_loop_refuses_a_submitted_call_and_closes_it() -> None:
    loop = nats._Loop()
    loop.close()
    call = asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="^bus is closed$"):
        loop.submit(call)
    assert inspect.getcoroutinestate(call) == inspect.CORO_CLOSED
