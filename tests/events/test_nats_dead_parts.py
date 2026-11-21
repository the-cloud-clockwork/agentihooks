import base64
import hashlib
import random
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from scripts.ci_lab import scheduler
from scripts.ci_lab.webhooks import HeadEvent, envelope
from scripts.events import nats
from scripts.events.config import BusConfig, QueuePolicy
from tests.events import nats_server

pytestmark = pytest.mark.unit


@pytest.mark.feature("lab-head-undecodable-dead-letter")
@pytest.mark.parametrize("migrated", [False, True], ids=["direct", "migrated"])
def test_full_size_dead_letters_recover_without_the_source_and_hide_parts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, migrated: bool
) -> None:
    async def refuse(message: nats.Msg) -> None:
        raise OSError("termination lost")

    with nats_server.running(tmp_path / "jetstream") as url:
        bus = nats.open_bus(BusConfig(url=url, queue=QueuePolicy(max_deliver=1, ack_wait_ms=20), dedupe_window_ms=100))
        try:
            payload = b"\xff" + random.Random(0).randbytes(bus._nc.max_payload - 1)
            consumer = bus.lab_heads.consumer(scheduler.DURABLE, scheduler.SUBJECT)
            published = bus._loop.run(bus._js.publish("ci.lab.head.push", payload))
            assert published.stream == nats.LAB_STREAM
            with monkeypatch.context() as patch:
                patch.setattr(nats.Msg, "term", refuse)
                with pytest.raises(OSError, match="^termination lost$"):
                    consumer.fetch(1, 100)
            before = bus._loop.run(bus._js.stream_info(nats.DEAD_STREAM)).state
            time.sleep(0.15)
            assert consumer.fetch(1, 100) == []
            after = bus._loop.run(bus._js.stream_info(nats.DEAD_STREAM)).state
            assert after.messages == before.messages
            bus._loop.run(bus._js.purge_stream(nats.LAB_STREAM))
            if migrated:
                for sequence in range(before.first_seq, before.last_seq + 1):
                    stored = bus._loop.run(bus._js.get_msg(nats.DEAD_STREAM, seq=sequence))
                    event = nats.Envelope.decode(stored.data)
                    target = f"dead.unknown.{hashlib.sha256(event.id.encode()).hexdigest()}"
                    bus._loop.run(bus._js.publish(target, stored.data, stream=nats.DEAD_STREAM))
                    bus._loop.run(bus._js.delete_msg(nats.DEAD_STREAM, sequence))
            reader = bus.dead.consumer("parts-proof", "dead.>")
            letters = reader.fetch(1, 100)
            assert len(letters) == 1
            assert letters[0].event.type == "dead_lettered"
            assert base64.b64decode(letters[0].event.data["event"]["data"]["payload"], validate=True) == payload
            letters[0].ack()
            assert reader.fetch(1, 100) == []
        finally:
            bus.close()


@pytest.mark.feature("lab-head-undecodable-dead-letter")
def test_the_part_type_is_an_ordinary_event_outside_the_dead_stream(tmp_path: Path) -> None:
    good = HeadEvent("push", "https://github.com/o/r", "a" * 40, "b" * 40, "dev", "pushed")
    event = replace(envelope(good), type="dead_payload_chunk")
    with nats_server.running(tmp_path / "jetstream") as url:
        bus = nats.open_bus(BusConfig(url=url))
        try:
            consumer = bus.lab_heads.consumer(scheduler.DURABLE, scheduler.SUBJECT)
            bus.lab_heads.publish("ci.lab.head.push", event)
            deliveries = consumer.fetch(1, 100)
            assert [delivery.event for delivery in deliveries] == [event]
            deliveries[0].ack()
            assert consumer.fetch(1, 100) == []
        finally:
            bus.close()


@pytest.mark.feature("lab-head-undecodable-dead-letter")
def test_ordinary_dead_letter_data_is_not_a_payload_manifest(tmp_path: Path) -> None:
    good = HeadEvent("push", "https://github.com/o/r", "a" * 40, "b" * 40, "dev", "pushed")
    event = replace(envelope(good), data={"payload_parts": ["invoice-item"]})
    with nats_server.running(tmp_path / "jetstream") as url:
        bus = nats.open_bus(BusConfig(url=url))
        try:
            bus._loop.run(nats._publish_dead(bus._js, event, "ordinary", 1))
            letters = bus.dead.consumer("opaque-data-proof", "dead.>").fetch(1, 100)
            assert len(letters) == 1
            assert letters[0].event.data["event"] == event.to_dict()
            letters[0].ack()
        finally:
            bus.close()


@pytest.mark.feature("lab-head-undecodable-dead-letter")
@pytest.mark.parametrize(("timeout_ms", "seconds"), [(1, 0.01), (100, 0.1)])
def test_plain_deliveries_keep_their_fetch_budget_without_stream_metadata(timeout_ms: int, seconds: float) -> None:
    good = HeadEvent("push", "https://github.com/o/r", "a" * 40, "b" * 40, "dev", "pushed")
    event = envelope(good)
    message = SimpleNamespace(data=event.encode(), metadata=SimpleNamespace(num_delivered=1), headers=None)
    subscription = SimpleNamespace(fetch=AsyncMock(return_value=[message]))
    loop = nats._Loop()
    try:
        consumer = nats.NatsConsumer(
            loop,
            cast(nats.JetStreamContext.PullSubscription, subscription),
            cast(nats.JetStreamContext, SimpleNamespace()),
            QueuePolicy(),
            "plain",
        )
        assert [delivery.event for delivery in consumer.fetch(1, timeout_ms)] == [event]
        subscription.fetch.assert_awaited_once_with(1, timeout=seconds)
    finally:
        loop.close()
