import base64
import time
from pathlib import Path

import pytest

from scripts.ci_lab import scheduler
from scripts.ci_lab.webhooks import HeadEvent, envelope
from scripts.events import nats
from scripts.events.config import BusConfig, QueuePolicy
from tests.events import nats_server

pytestmark = pytest.mark.unit


@pytest.mark.feature("lab-head-undecodable-dead-letter")
@pytest.mark.parametrize("payload", [b"{", b"\xff", b"[]", b"{}"], ids=["json", "utf8", "array", "fields"])
def test_an_undecodable_head_is_dead_lettered_and_the_next_head_is_read(payload: bytes, tmp_path: Path) -> None:
    policy = QueuePolicy(max_deliver=1, ack_wait_ms=20)
    good = HeadEvent("push", "https://github.com/o/r", "a" * 40, "b" * 40, "dev", "pushed")
    with nats_server.running(tmp_path / "jetstream") as url:
        bus = nats.open_bus(BusConfig(url=url, queue=policy))
        try:
            consumer = bus.lab_heads.consumer(scheduler.DURABLE, scheduler.SUBJECT)
            inbox = scheduler.BusInbox(consumer, batch=2, timeout_ms=100)
            published = bus._loop.run(bus._js.publish("ci.lab.head.push", payload, stream=nats.LAB_STREAM))
            bus.lab_heads.publish("ci.lab.head.push", envelope(good))
            errors = []
            heads = []
            for _ in range(3):
                try:
                    heads.extend(inbox.heads())
                except ValueError as error:
                    errors.append(type(error).__name__)
                    time.sleep(0.04)
            info = bus._loop.run(bus._js.consumer_info(nats.LAB_STREAM, nats.consumer_name(scheduler.DURABLE)))
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
            assert original["data"]["subject"] == "ci.lab.head.push"
            assert base64.b64decode(original["data"]["payload"], validate=True) == payload
            letters[0].ack()
            assert inbox.heads() == []
            assert reader.fetch(1, 100) == []
        finally:
            bus.close()
