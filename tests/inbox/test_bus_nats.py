import pytest

from scripts.events.config import BusConfig, QueuePolicy
from scripts.events.factory import open_bus
from tests.events.nats_server import running

pytestmark = pytest.mark.xdist_group("nats-inbox")


def test_a_seat_reopens_the_same_server_consumer_and_only_confirmation_acks(tmp_path):
    from scripts.inbox.bus import Message, SeatInbox

    with running(tmp_path / "nats") as url:
        config = BusConfig(backend="nats", url=url, queue=QueuePolicy(ack_wait_ms=50))
        first_bus = open_bus(config)
        try:
            mail = SeatInbox(first_bus, "swarm")
            consumer = mail.consumer("eng-1@swarm", "old")
            sent = mail.send("eng-1@swarm", Message("stable", "sender", "work"))
            first = consumer.fetch(1, 1000)[0]
            assert first.event == sent
            durable = consumer.durable
        finally:
            first_bus.close()
        second_bus = open_bus(config)
        try:
            mail = SeatInbox(second_bus, "swarm")
            successor = mail.consumer("eng-1@swarm", "new")
            assert successor.durable == durable
            assert mail.consumer("eng-2@swarm", "new").fetch(1, 0) == []
            second = successor.fetch(1, 1000)[0]
            assert second.event == first.event
            assert not successor.confirm(first)
            assert successor.confirm(second)
            assert successor.fetch(1, 100) == []
            consumers = second_bus._loop.run(second_bus._js.consumers_info("AGENTIHOOKS_EVENTS"))
            assert {entry.name for entry in consumers} == {durable, mail.consumer("eng-2@swarm", "new").durable}
        finally:
            second_bus.close()
