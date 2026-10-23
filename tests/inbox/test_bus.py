from dataclasses import replace
from unittest.mock import patch

import pytest

from scripts.events import memory
from scripts.events.config import BusConfig, QueuePolicy


@pytest.fixture
def rig():
    clock = [0]
    bus = memory.open_bus(BusConfig(queue=QueuePolicy(ack_wait_ms=10)), clock=lambda: clock[0])
    yield bus, clock
    bus.close()


def inbox(bus):
    from scripts.inbox.bus import SeatInbox

    return SeatInbox(bus, "swarm")


def message(**changes):
    from scripts.inbox.bus import Message

    return Message(**{"id": "stable", "sender": "sender", "text": "work", **changes})


def test_one_consumer_is_shared_by_successor_sessions_and_seats_are_isolated(rig):
    bus, _ = rig
    mail = inbox(bus)
    first = mail.consumer("eng-1@swarm", "first")
    successor = mail.consumer("eng-1@swarm", "next")
    other = mail.consumer("eng-2@swarm", "first")
    assert first.durable == successor.durable
    assert first.subject == successor.subject
    assert first.durable != other.durable
    mail.send("eng-1@swarm", message())
    assert other.fetch(1, 0) == []
    receipt = first.fetch(1, 0)[0]
    assert receipt.event.id == "stable"
    assert successor.fetch(1, 0) == []
    assert len(bus.events._consumers) == 2


def test_delivery_is_unacked_until_its_receiving_session_confirms(rig):
    bus, clock = rig
    mail = inbox(bus)
    first = mail.consumer("seat", "first")
    mail.send("seat", message())
    receipt = first.fetch(1, 0)[0]
    clock[0] += 11
    next_session = inbox(bus).consumer("seat", "next")
    redelivered = next_session.fetch(1, 0)[0]
    assert redelivered.event == receipt.event
    assert redelivered.revision > receipt.revision
    assert not first.confirm(receipt)
    assert not next_session.confirm(receipt)
    assert next_session.confirm(redelivered)
    clock[0] += 11
    assert first.fetch(1, 0) == []


def test_send_retries_keep_the_original_event_and_refuse_changed_content(rig):
    bus, clock = rig
    mail = inbox(bus)
    sent = mail.send("seat", message())
    clock[0] += 120_001
    assert mail.send("seat", message()) == sent
    with pytest.raises(ValueError, match="different message"):
        mail.send("seat", message(text="changed"))
    with pytest.raises(ValueError, match="different message"):
        mail.send("another", message())
    consumer = mail.consumer("seat", "session")
    receipt = consumer.fetch(1, 0)[0]
    assert consumer.confirm(receipt)
    assert consumer.fetch(1, 0) == []


def test_a_publish_failure_can_be_retried_with_the_same_id(rig):
    bus, _ = rig
    mail = inbox(bus)
    with patch.object(bus.events, "publish", side_effect=OSError("offline")):
        with pytest.raises(OSError, match="offline"):
            mail.send("seat", message())
    event = inbox(bus).send("seat", message())
    assert event.id == "stable"
    assert mail.consumer("seat", "session").fetch(1, 0)[0].event == event


def test_confirmation_survives_a_lost_ack_and_replay_is_not_shown(rig):
    bus, clock = rig
    mail = inbox(bus)
    consumer = mail.consumer("seat", "session")
    mail.send("seat", message())
    receipt = consumer.fetch(1, 0)[0]
    with patch.object(receipt.delivery, "ack", side_effect=OSError("lost ack")):
        with pytest.raises(OSError, match="lost ack"):
            consumer.confirm(receipt)
    clock[0] += 11
    assert inbox(bus).consumer("seat", "successor").fetch(1, 0) == []
    clock[0] += 11
    assert consumer.fetch(1, 0) == []


def test_a_forged_confirmation_cannot_change_the_claim(rig):
    bus, _ = rig
    mail = inbox(bus)
    consumer = mail.consumer("seat", "session")
    mail.send("seat", message())
    receipt = consumer.fetch(1, 0)[0]
    assert not consumer.confirm(replace(receipt, revision=receipt.revision + 1))
    assert not consumer.confirm(replace(receipt, session="another"))
    assert consumer.confirm(receipt)
