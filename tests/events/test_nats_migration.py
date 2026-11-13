from typing import cast

import pytest

from scripts.events import nats
from scripts.events.bus import Bus
from scripts.events.config import BusConfig, QueuePolicy
from scripts.events.envelope import Envelope
from scripts.swarm import readiness
from scripts.swarm_ledger.repository import hierarchy
from tests.hive.test_nats_grant import _user_creds
from tests.hive.test_nats_grant import signer as signer

pytestmark = pytest.mark.unit


@pytest.fixture
def buses(signer):
    credential = _user_creds(signer.account, "migration")
    legacy = nats.open_bus(BusConfig(backend="nats", url=signer.url, credentials=credential))
    scoped = nats.open_bus(BusConfig(backend="nats", url=signer.url, credentials=credential, scoped=True))
    from tests.events.test_nats_state import guard_state_reads

    guard_state_reads(scoped._js)
    try:
        yield legacy, scoped
    finally:
        scoped.close()
        legacy.close()


def test_state_migration_preserves_owned_history_and_revision_fences(buses):
    legacy, hub = buses
    old = legacy.state("spoke-work")
    original = old.create("owned", b"first")
    old.create("foreign", b"never copy")
    current = old.update("owned", b"second", original)
    hub.migrate_state("rig", "spoke-work", ["owned"])
    target = hub.for_swarm("rig").state("spoke-work")
    assert target.get("owned").revision == current
    assert target.get("owned").value == b"second"
    assert target.get("foreign") is None
    assert [(entry.value, entry.revision) for entry in target.watch("owned")] == [
        (b"first", original),
        (b"second", current),
    ]
    hub.migrate_state("rig", "spoke-work", ["owned"])
    assert [entry.value for entry in target.watch("owned")] == [b"first", b"second"]
    updated = target.update("owned", b"third", current)
    assert updated > current
    assert [entry.value for entry in target.watch("owned", updated)] == [b"third"]
    assert old.get("owned").value == b"second"


def test_queue_migration_preserves_pending_work_and_attempt_number(buses):
    legacy, hub = buses
    policy = QueuePolicy(max_deliver=3, ack_wait_ms=100)
    work = Envelope("retained", "task.ready", "rig", "task", "work", 1, 0, "hub", {})
    original = legacy.queue("swarm.rig.ready.eng", policy)
    original.offer(work)
    original.take(1000).nak()
    hub.migrate_queue("rig", "swarm.rig.ready.eng", policy)
    migrated = hub.for_swarm("rig").queue("eng", policy).take(1000)
    assert migrated.event == work
    assert migrated.attempt == 2
    migrated.ack()
    assert original.take(20) is None


@pytest.mark.parametrize("swarm", ["rig", "doctor"])
@pytest.mark.parametrize(("lane", "count"), [("eng", 3), ("ci", 7), ("plan", 2)])
def test_queue_migration_keeps_every_retained_offer_when_repeated(buses, swarm, lane, count):
    legacy, hub = buses
    policy = QueuePolicy(max_deliver=3, ack_wait_ms=100)
    name = f"swarm.{swarm}.ready.{lane}"
    original = legacy.queue(name, policy)
    offers = [
        Envelope(f"retained-{index}", "task.ready", swarm, "task", str(index), 1, 0, "hub", {"lane": lane})
        for index in range(count)
    ]
    for offer in offers:
        original.offer(offer)

    hub.migrate_queue(swarm, name, policy)
    hub.migrate_queue(swarm, name, policy)

    target = hub.for_swarm(swarm).queue(lane, policy)
    received = []
    for _ in offers:
        delivery = target.take(1000)
        assert delivery is not None
        received.append(delivery.event)
        delivery.ack()
    assert received == offers
    assert target.take(20) is None
    assert original.take(20) is None


def test_dead_letter_migration_keeps_retained_letters(buses):
    legacy, hub = buses
    letter = Envelope("letter", "dead_lettered", "rig", "task", "work", 1, 0, "hub", {})
    foreign = Envelope("foreign", "dead_lettered", "other", "task", "work", 1, 0, "hub", {})
    legacy.dead.publish("dead.rig", letter)
    legacy.dead.publish("dead.other", foreign)
    read = hub._js.get_msg
    seen = []

    async def advancing_read(stream, *, seq):
        assert seq == len(seen) + 1
        seen.append(seq)
        return await read(stream, seq=seq)

    hub._js.get_msg = advancing_read
    hub.migrate_dead("rig")
    assert seen == [1, 2]
    target = hub.for_swarm("rig").dead.consumer("migrated", "dead.>")
    deliveries = target.fetch(10, 1000)
    assert [delivery.event for delivery in deliveries] == [letter]


def test_scoped_state_requires_explicit_legacy_key_ownership(buses):
    legacy, hub = buses
    legacy.state("spoke-work").create("unknown-owner", b"retained")
    with pytest.raises(RuntimeError, match="ownership"):
        hub.for_swarm("rig").state("spoke-work")
    hub.migrate_state("rig", "spoke-work", [])
    assert hub.for_swarm("rig").state("spoke-work").get("unknown-owner") is None
    assert legacy.state("spoke-work").get("unknown-owner").value == b"retained"


@pytest.mark.feature("hub-fan-in-unowned-state")
def test_hub_fan_in_keeps_other_swarms_while_one_swarm_state_is_unowned(buses, capsys):
    legacy, hub = buses
    legacy.state("spoke-work").create("unknown-owner", b"retained")
    hub.migrate_state("rig", "spoke-work", [])
    with pytest.raises(RuntimeError, match="swarm 'stuck' bucket 'spoke-work'"):
        hub.for_swarm("stuck")
    consumer = hub.events.consumer("aggregate", "swarm.*.execution.started")
    work = Envelope("own", "execution.started", "rig", "execution", "work", 1, 0, "member", {})
    assert hub.events.publish("swarm.rig.execution.started", work)
    assert [delivery.event for delivery in consumer.fetch(10, 1000)] == [work]
    assert (
        "hub: swarm 'stuck' skipped: legacy state requires explicit key ownership: swarm 'stuck' bucket 'spoke-work'"
        in capsys.readouterr().err
    )


@pytest.mark.feature("hub-fan-in-unowned-state")
def test_hub_offers_nothing_for_a_swarm_with_unowned_legacy_inbox_until_it_is_migrated(
    buses: tuple[nats.NatsBus, nats.NatsBus],
) -> None:
    legacy, hub = buses
    legacy.state("inbox").create("unknown-owner", b"retained")
    work = {"id": "work", "title": "work", "phase": "p1", "state": "open", "lane": "eng"}
    found = {"overview": "o", "tasks": [work], "phases": [{"id": "p1", "title": "build", "depends_on": []}]}
    view = readiness.Snapshot(
        "rig", {"work": work}, found, hierarchy.project(found)[0], "", lambda task_id: False, lambda task: True
    )
    bus = cast(Bus, hub)
    with pytest.raises(RuntimeError, match="swarm 'rig' bucket 'inbox'"):
        readiness.publish(bus, view, 0)
    hub.migrate_state("rig", "inbox", [])
    assert hub.for_swarm("rig").queue("eng", QueuePolicy()).take(20) is None
    assert readiness.publish(bus, view, 0) == ["ready:rig:work:0"]
    delivery = hub.for_swarm("rig").queue("eng", QueuePolicy()).take(1000)
    assert delivery is not None
    assert delivery.event.id == "ready:rig:work:0"
