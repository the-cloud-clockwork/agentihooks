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
    assert ".inbox." in first.subject
    assert first.durable != other.durable
    mail.send("eng-1@swarm", message())
    assert other.fetch(1, 0) == []
    receipt = first.fetch(1, 0)[0]
    assert receipt.event.id == "stable"
    assert receipt.session == "first"
    assert mail.state is bus.state("inbox")
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


def test_confirmation_can_be_retried_after_its_ack_failed(rig):
    bus, _ = rig
    consumer = inbox(bus).consumer("seat", "session")
    inbox(bus).send("seat", message())
    receipt = consumer.fetch(1, 0)[0]
    with patch.object(receipt.delivery, "ack", side_effect=OSError("lost ack")):
        with pytest.raises(OSError, match="lost ack"):
            consumer.confirm(receipt)
    assert not consumer.confirm(replace(receipt, revision=receipt.revision + 1))
    assert consumer.confirm(receipt)


def test_conflicting_claims_and_confirmations_do_not_ack_unconfirmed_work(rig):
    from scripts.events.bus import StateConflict

    bus, clock = rig
    mail = inbox(bus)
    consumer = mail.consumer("seat", "session")
    mail.send("seat", message())
    with (
        patch.object(mail.state, "create", side_effect=StateConflict),
        patch.object(consumer._consumer, "fetch", wraps=consumer._consumer.fetch) as fetch,
    ):
        assert consumer.fetch(1, 0) == []
        fetch.assert_called_once_with(1, 0)
    clock[0] += 11
    receipt = consumer.fetch(1, 0)[0]
    with patch.object(mail.state, "update", side_effect=StateConflict):
        assert not consumer.confirm(receipt)
    clock[0] += 11
    newer = consumer.fetch(1, 0)[0]
    assert not consumer.confirm(receipt)
    assert consumer.confirm(newer)


def test_repeated_delivery_of_one_attempt_cannot_be_claimed_twice(rig):
    bus, _ = rig
    mail = inbox(bus)
    first = mail.consumer("seat", "first")
    other = mail.consumer("seat", "other")
    mail.send("seat", message())
    receipt = first.fetch(1, 0)[0]
    with patch.object(other._consumer, "fetch", return_value=[receipt.delivery]):
        assert other.fetch(1, 0) == []
    assert first.confirm(receipt)


def test_receipts_cannot_be_confirmed_for_another_mailbox_or_unknown_item(rig):
    bus, _ = rig
    mail = inbox(bus)
    consumer = mail.consumer("seat", "session")
    mail.send("seat", message())
    receipt = consumer.fetch(1, 0)[0]
    for event in (
        replace(receipt.event, swarm="another"),
        replace(receipt.event, key="another"),
        replace(receipt.event, id="unknown"),
    ):
        assert not consumer.confirm(replace(receipt, event=event))
    assert consumer.confirm(receipt)


@pytest.mark.parametrize(
    "swarm,address,session", [("", "seat", "session"), ("swarm", "", "session"), ("swarm", "seat", "")]
)
def test_a_consumer_refuses_missing_identity(rig, swarm, address, session):
    from scripts.inbox.bus import SeatInbox

    bus, _ = rig
    expected = "a seat inbox needs a swarm" if not swarm else "a consumer needs a seat address and receiving session"
    with pytest.raises(ValueError) as error:
        SeatInbox(bus, swarm).consumer(address, session)
    assert str(error.value) == expected


@pytest.mark.parametrize("address,text", [("", "work"), ("seat", "   ")])
def test_send_refuses_empty_address_or_blank_text(rig, address, text):
    bus, _ = rig
    with pytest.raises(ValueError) as error:
        inbox(bus).send(address, message(text=text))
    assert str(error.value) == "a message needs an address and text"


def test_the_saved_envelope_preserves_the_full_message_and_first_timestamp(rig):
    bus, _ = rig
    mail = inbox(bus)
    item = message(ref="reference", fyi=True, task="task")
    with patch("scripts.inbox.bus.time.time_ns", return_value=123_000_000):
        event = mail.send("seat", item)
    assert event.to_dict() == {
        "id": "stable",
        "type": "inbox.sent",
        "swarm": "swarm",
        "entity": "inbox",
        "key": "seat",
        "version": 1,
        "time": 123,
        "author": "sender",
        "data": {"id": "stable", "sender": "sender", "text": "work", "ref": "reference", "fyi": True, "task": "task"},
        "cause": None,
        "schema_version": 1,
    }
    with patch("scripts.inbox.bus.time.time_ns", return_value=456_000_000):
        assert inbox(bus).send("seat", item) == event
    with pytest.raises(ValueError) as error:
        mail.send("seat", replace(item, ref="changed"))
    assert str(error.value) == "item id already belongs to a different message"


def test_swarms_have_distinct_durables_and_send_ids_cannot_cross_swarms(rig):
    from scripts.inbox.bus import SeatInbox

    bus, _ = rig
    first = SeatInbox(bus, "one")
    second = SeatInbox(bus, "two")
    left = first.consumer("seat", "same")
    right = second.consumer("seat", "same")
    assert left.durable != right.durable
    assert left.subject != right.subject
    first.send("seat", message())
    with pytest.raises(ValueError) as error:
        second.send("seat", message())
    assert str(error.value) == "item id already belongs to a different message"
    assert right.fetch(1, 0) == []
    second.send("seat", message(id="other"))
    assert left.fetch(1, 0)[0].event.id == "stable"
    assert right.fetch(1, 0)[0].event.id == "other"


def test_altering_a_receipt_address_cannot_confirm_another_mailbox(rig):
    from scripts.inbox.bus import SeatInbox

    bus, _ = rig
    mail = inbox(bus)
    mail.send("seat", message())
    original = mail.consumer("seat", "session")
    receipt = original.fetch(1, 0)[0]
    another_seat = mail.consumer("other", "session")
    assert not another_seat.confirm(replace(receipt, event=replace(receipt.event, key="other")))
    another_swarm = SeatInbox(bus, "other").consumer("seat", "session")
    assert not another_swarm.confirm(replace(receipt, event=replace(receipt.event, swarm="other")))
    assert original.confirm(receipt)


def test_each_message_in_a_seat_batch_has_its_own_claim(rig):
    bus, _ = rig
    mail = inbox(bus)
    mail.send("seat", message(id="first"))
    mail.send("seat", message(id="second"))
    consumer = mail.consumer("seat", "session")
    receipts = consumer.fetch(2, 0)
    assert [receipt.event.id for receipt in receipts] == ["first", "second"]
    assert all(consumer.confirm(receipt) for receipt in receipts)


def test_a_redelivery_claim_conflict_keeps_work_available(rig):
    from scripts.events.bus import StateConflict

    bus, clock = rig
    mail = inbox(bus)
    mail.send("seat", message())
    consumer = mail.consumer("seat", "session")
    original = consumer.fetch(1, 0)[0]
    clock[0] += 11
    with patch.object(mail.state, "update", side_effect=StateConflict):
        assert consumer.fetch(1, 0) == []
    clock[0] += 11
    latest = consumer.fetch(1, 0)[0]
    assert not consumer.confirm(original)
    assert consumer.confirm(latest)


def test_a_lost_publish_response_does_not_repeat_confirmed_work(rig):
    bus, clock = rig
    mail = inbox(bus)
    publish = bus.events.publish

    def lose_response(subject, event):
        publish(subject, event)
        raise OSError("response lost")

    with patch.object(bus.events, "publish", side_effect=lose_response):
        with pytest.raises(OSError, match="response lost"):
            mail.send("seat", message())
    consumer = mail.consumer("seat", "session")
    receipt = consumer.fetch(1, 0)[0]
    assert consumer.confirm(receipt)
    clock[0] += 120_001
    assert mail.send("seat", message()) == receipt.event
    assert consumer.fetch(1, 0) == []
