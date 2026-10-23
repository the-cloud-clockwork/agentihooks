import pytest

from scripts.events import nats
from scripts.events.config import BusConfig, QueuePolicy
from scripts.events.envelope import Envelope
from tests.hive.test_nats_grant import _user_creds
from tests.hive.test_nats_grant import signer as signer

pytestmark = pytest.mark.unit


@pytest.fixture
def buses(signer):
    credential = _user_creds(signer.account, "migration")
    legacy = nats.open_bus(BusConfig(backend="nats", url=signer.url, credentials=credential))
    scoped = nats.open_bus(BusConfig(backend="nats", url=signer.url, credentials=credential, scoped=True))
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


def test_dead_letter_migration_keeps_retained_letters(buses):
    legacy, hub = buses
    letter = Envelope("letter", "dead_lettered", "rig", "task", "work", 1, 0, "hub", {})
    foreign = Envelope("foreign", "dead_lettered", "other", "task", "work", 1, 0, "hub", {})
    legacy.dead.publish("dead.rig", letter)
    legacy.dead.publish("dead.other", foreign)
    hub.migrate_dead("rig")
    target = hub.for_swarm("rig").dead.consumer("migrated", "dead.>")
    deliveries = target.fetch(10, 1000)
    assert [delivery.event for delivery in deliveries] == [letter]
