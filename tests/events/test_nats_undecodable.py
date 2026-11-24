import base64
import random
import time
from pathlib import Path
from typing import cast

import pytest

from scripts.ci_lab import scheduler
from scripts.ci_lab.webhooks import HeadEvent, envelope
from scripts.events import nats
from scripts.events.config import BusConfig, QueuePolicy
from tests.events import nats_server

pytestmark = pytest.mark.unit


@pytest.mark.feature("lab-head-undecodable-dead-letter")
@pytest.mark.parametrize(
    "payload",
    [b"{", b"\xff", b"[]", b"{}", b"[" * 10000 + b"]" * 10000, b"", b"full-limit"],
    ids=["json", "utf8", "array", "fields", "depth", "near-limit", "full-limit"],
)
def test_an_undecodable_head_is_dead_lettered_and_the_next_head_is_read(payload: bytes, tmp_path: Path) -> None:
    policy = QueuePolicy(max_deliver=1, ack_wait_ms=20)
    good = HeadEvent("push", "https://github.com/o/r", "a" * 40, "b" * 40, "dev", "pushed")
    with nats_server.running(tmp_path / "jetstream") as url:
        bus = nats.open_bus(BusConfig(url=url, queue=policy))
        try:
            if not payload:
                payload = b"\xff" * (bus._nc.max_payload * 9 // 10)
            elif payload == b"full-limit":
                payload = b"\xff" + random.Random(0).randbytes(bus._nc.max_payload - 1)
            consumer = bus.lab_heads.consumer(scheduler.DURABLE, scheduler.SUBJECT)
            inbox = scheduler.BusInbox(cast(scheduler.Consumer, consumer), batch=2, timeout_ms=100)
            published = bus._loop.run(bus._js.publish("ci.lab.head.push", payload))
            assert published.stream == nats.LAB_STREAM
            bus.lab_heads.publish("ci.lab.head.push", envelope(good))
            errors = []
            heads = []
            for _ in range(3):
                try:
                    heads.extend(inbox.heads())
                except (ValueError, RecursionError, nats.nats_payload.PayloadTooLarge) as error:
                    errors.append(type(error).__name__)
                    time.sleep(0.04)
            info = bus._loop.run(bus._js.consumer_info(nats.LAB_STREAM, nats.consumer_name(scheduler.DURABLE)))
            bus._loop.run(bus._js.purge_stream(nats.LAB_STREAM))
            reader = bus.dead.consumer("malformed-proof", "dead.>")
            letters = reader.fetch(3, 100)
            print(
                f"decode_errors={errors} redelivered={info.num_redelivered} "
                f"pending={info.num_ack_pending} dead_letters={len(letters)}"
            )
            assert not errors, f"undecodable lab head was redelivered: {errors}; pending={info.num_ack_pending}"
            assert [head.event for head in heads] == [good]
            assert info.num_ack_pending == 0
            assert info.num_pending == 0
            assert len(letters) == 1
            letter = letters[0].event
            assert letter.type == "dead_lettered"
            assert letter.data["source"] == f"events.{scheduler.DURABLE}"
            assert letter.data["attempts"] == 1
            original = letter.data["event"]
            assert original["id"] == f"{nats.LAB_STREAM}:{published.seq}"
            assert letter.cause == original["id"]
            assert original["type"] == "undecodable"
            assert original["swarm"] == "unknown"
            assert original["entity"] == "message"
            assert original["key"] == "ci.lab.head.push"
            assert original["version"] == 0
            assert original["time"] == 0
            assert original["author"] == "events"
            assert original["data"]["subject"] == "ci.lab.head.push"
            assert base64.b64decode(original["data"]["payload"], validate=True) == payload
            letters[0].ack()
            assert inbox.heads() == []
            assert reader.fetch(1, 100) == []
        finally:
            bus.close()


@pytest.mark.feature("lab-head-undecodable-dead-letter")
def test_an_undecodable_head_stays_pending_when_dead_letter_publication_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(*args: object, **kwargs: object) -> None:
        raise OSError("dead stream unavailable")

    with nats_server.running(tmp_path / "jetstream") as url:
        bus = nats.open_bus(BusConfig(url=url, queue=QueuePolicy(max_deliver=1, ack_wait_ms=20)))
        try:
            consumer = bus.lab_heads.consumer(scheduler.DURABLE, scheduler.SUBJECT)
            bus._loop.run(bus._js.publish("ci.lab.head.push", b"{", stream=nats.LAB_STREAM))
            with monkeypatch.context() as patch:
                patch.setattr(nats.nats_payload, "publish", refuse)
                with pytest.raises(OSError, match="^dead stream unavailable$"):
                    consumer.fetch(1, 100)
            info = bus._loop.run(bus._js.consumer_info(nats.LAB_STREAM, nats.consumer_name(scheduler.DURABLE)))
            assert info.num_ack_pending == 1
            assert bus._loop.run(bus._js.stream_info(nats.DEAD_STREAM)).state.messages == 0
            time.sleep(0.04)
            assert consumer.fetch(1, 100) == []
            info = bus._loop.run(bus._js.consumer_info(nats.LAB_STREAM, nats.consumer_name(scheduler.DURABLE)))
            assert info.num_ack_pending == 0
            reader = bus.dead.consumer("publish-refused-proof", "dead.>")
            letters = reader.fetch(2, 100)
            assert len(letters) == 1
            assert base64.b64decode(letters[0].event.data["event"]["data"]["payload"], validate=True) == b"{"
            letters[0].ack()
        finally:
            bus.close()


@pytest.mark.feature("lab-head-undecodable-dead-letter")
def test_an_undecodable_head_has_one_retained_letter_when_termination_is_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(message: nats.Msg) -> None:
        assert message.data == b"\xff"
        raise OSError("termination lost")

    with nats_server.running(tmp_path / "jetstream") as url:
        bus = nats.open_bus(BusConfig(url=url, queue=QueuePolicy(max_deliver=1, ack_wait_ms=20), dedupe_window_ms=100))
        try:
            consumer = bus.lab_heads.consumer(scheduler.DURABLE, scheduler.SUBJECT)
            published = bus._loop.run(bus._js.publish("ci.lab.head.push", b"\xff", stream=nats.LAB_STREAM))
            with monkeypatch.context() as patch:
                patch.setattr(nats.Msg, "term", refuse)
                with pytest.raises(OSError, match="^termination lost$"):
                    consumer.fetch(1, 100)
            info = bus._loop.run(bus._js.consumer_info(nats.LAB_STREAM, nats.consumer_name(scheduler.DURABLE)))
            assert info.num_ack_pending == 1
            assert bus._loop.run(bus._js.stream_info(nats.DEAD_STREAM)).state.messages == 1
            time.sleep(0.15)
            assert consumer.fetch(1, 100) == []
            info = bus._loop.run(bus._js.consumer_info(nats.LAB_STREAM, nats.consumer_name(scheduler.DURABLE)))
            assert info.num_ack_pending == 0
            assert info.num_pending == 0
            reader = bus.dead.consumer("term-lost-proof", "dead.>")
            letters = reader.fetch(2, 100)
            assert len(letters) == 1
            letter = letters[0].event
            assert letter.cause == f"{nats.LAB_STREAM}:{published.seq}"
            assert letter.data["attempts"] == 1
            assert base64.b64decode(letter.data["event"]["data"]["payload"], validate=True) == b"\xff"
            letters[0].ack()
            assert reader.fetch(1, 100) == []
        finally:
            bus.close()
