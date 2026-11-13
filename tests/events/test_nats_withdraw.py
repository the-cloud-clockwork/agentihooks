from collections.abc import Iterator
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import pytest
from nats.js.errors import ServerError

from scripts.events import nats
from scripts.events.config import BusConfig, QueuePolicy
from scripts.events.envelope import Envelope
from tests.events import nats_server


def offer(key: str) -> Envelope:
    return Envelope(
        id=key, type="task.ready", swarm="rig", entity="task", key=key, version=1, time=1, author="hub", data={}
    )


@pytest.fixture
def bus(tmp_path: Path) -> Iterator[nats.NatsBus]:
    with nats_server.running(tmp_path / "nats") as url:
        opened = nats.open_bus(BusConfig(backend="nats", url=url))
        try:
            yield opened
        finally:
            opened.close()


def remove(bus: nats.NatsBus, queue: nats.NatsWorkQueue, action: str) -> None:
    delivery = queue.take(1000)
    assert delivery is not None
    if action == "ack":
        bus._loop.run(delivery._msg.ack_sync())
    else:
        delivery.term()
        bus._loop.run(bus._nc.flush())
    info = bus._loop.run(bus._js.stream_info(queue._stream))
    assert info.state.messages == 0


@pytest.mark.parametrize("action", ["ack", "term"])
def test_consumer_removal_after_get_replays_the_live_delete_error(bus: nats.NatsBus, action: str) -> None:
    queue = cast(nats.NatsWorkQueue, bus.queue("ready.rig.eng", QueuePolicy()))
    assert queue.offer(offer("old"))
    info = bus._loop.run(bus._js.stream_info(queue._stream))
    assert info.config.retention == nats.api.RetentionPolicy.WORK_QUEUE
    message = bus._loop.run(queue._get(info.state.first_seq))
    assert message is not None
    assert Envelope.decode(message.data) == offer("old")
    remove(bus, queue, action)
    with pytest.raises(ServerError) as raised:
        bus._loop.run(bus._js.delete_msg(queue._stream, info.state.first_seq))
    assert raised.value.code == 500
    assert raised.value.err_code == 10057
    assert raised.value.description == "no message found"


@pytest.mark.feature("nats-withdraw-gone-message")
@pytest.mark.parametrize("action", ["ack", "term"])
def test_withdraw_continues_after_a_consumer_removes_the_read_offer(bus: nats.NatsBus, action: str) -> None:
    queue = cast(nats.NatsWorkQueue, bus.queue("ready.rig.eng", QueuePolicy()))
    assert queue.offer(offer("old"))

    def stale(event: Envelope) -> bool:
        assert event == offer("old")
        remove(bus, queue, action)
        return True

    assert queue.withdraw(stale) == 0
    assert queue.offer(offer("fresh"))
    delivery = queue.take(1000)
    assert delivery is not None
    assert delivery.event == offer("fresh")
    delivery.ack()


@pytest.mark.unit
@pytest.mark.parametrize("error_code", [10057, 10059])
def test_withdraw_tolerates_only_a_missing_message_delete(error_code: int) -> None:
    error = ServerError(code=500, err_code=error_code, description="delete failed")
    queue = object.__new__(nats.NatsWorkQueue)
    queue._stream = "queue"
    queue._js = AsyncMock()
    queue._js.get_msg.return_value = nats.api.RawStreamMsg(data=offer("old").encode())
    queue._js.delete_msg.side_effect = error
    queue._loop = nats._Loop()
    try:
        if error_code == 10057:
            assert queue._withdrawn(7, lambda event: True) is False
        else:
            with pytest.raises(ServerError) as raised:
                queue._withdrawn(7, lambda event: True)
            assert raised.value is error
        queue._js.delete_msg.assert_awaited_once_with("queue", 7)
    finally:
        queue._loop.close()
