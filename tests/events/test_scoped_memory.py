import pytest

from scripts.events import memory
from scripts.events.config import BusConfig, QueuePolicy
from scripts.events.envelope import Envelope

pytestmark = pytest.mark.unit


def work(swarm):
    return Envelope("same-id", "task.ready", swarm, "task", "work", 1, 0, "member", {})


def test_explicit_scoped_memory_views_isolate_work_and_state():
    bus = memory.open_bus(BusConfig(scoped=True))
    try:
        first, second = bus.for_swarm("first"), bus.for_swarm("second")
        assert bus.for_swarm("first") is first
        first.state("claims").create("key", b"first")
        second.state("claims").create("key", b"second")
        assert first.state("claims").get("key").value == b"first"
        assert second.state("claims").get("key").value == b"second"
        for name, scoped in [("first", first), ("second", second)]:
            assert scoped.queue("eng", QueuePolicy()).offer(work(name))
            assert scoped.queue("eng", QueuePolicy()).take(0).event == work(name)
        first.close()
        assert second.state("claims").get("key").value == b"second"
    finally:
        bus.close()
    with pytest.raises(RuntimeError, match="bus is closed"):
        second.state("claims").get("key")


def test_legacy_memory_scope_selection_keeps_existing_shared_storage():
    bus = memory.open_bus(BusConfig())
    try:
        assert bus.for_swarm("first") is bus
        assert bus.for_swarm("second") is bus
    finally:
        bus.close()


def test_scoped_memory_aggregate_events_and_dead_include_legacy_and_owned_logs():
    from scripts.events.memory import MemoryBus

    bus = memory.open_bus(BusConfig(scoped=True))
    assert isinstance(bus, MemoryBus)
    try:
        own = bus.for_swarm("rig")
        other = bus.for_swarm("other")
        for name, logical in [("events", "swarm.>"), ("dead", "dead.>")]:
            aggregate = getattr(bus, name)
            consumer = aggregate.consumer(name, logical)
            assert aggregate.publish("ready", work("rig"))
            assert getattr(other, name).publish("ready", work("other"))
            legacy = getattr(bus, "_legacy_" + name)
            assert legacy.publish("dead.old" if name == "dead" else "swarm.old.ready", work("old"))
            delivered = consumer.fetch(3, 100)
            assert {entry.event.swarm for entry in delivered} == {"rig", "other", "old"}
            raw = getattr(own, "_legacy_" + name)
            assert raw.entries[0][0] == "swarm.rig." + name + ".ready"
    finally:
        bus.close()


def test_scoped_memory_queue_root_routing_and_immutable_views():
    from scripts.events.memory import MemoryBus

    policy = QueuePolicy(max_deliver=2)
    bus = memory.open_bus(BusConfig(scoped=True))
    assert isinstance(bus, MemoryBus)
    try:
        own = bus.for_swarm("rig")
        queue = bus.queue("swarm.rig.ready.eng", policy)
        assert own.queue("eng", policy) is queue
        assert own.queue("swarm.rig.ready.eng", policy) is queue
        assert queue.policy == policy
        assert queue.offer(work("rig"))
        assert queue.take(10).event == work("rig")
        other_lane = own.queue("ci", policy)
        assert other_lane is not queue
        assert other_lane.offer(work("rig"))
        assert queue.take(1) is None
        assert other_lane.take(10).event == work("rig")
        assert own.for_swarm("rig") is own
        with pytest.raises(ValueError) as caught:
            own.for_swarm("other")
        assert str(caught.value) == "a scoped bus cannot change its swarm"
        for name in ["eng", "swarm.rig.ready.eng.extra", "other.rig.ready.eng", "swarm.rig.other.eng"]:
            with pytest.raises(ValueError) as caught:
                bus.queue(name, policy)
            assert str(caught.value) == "select a swarm before opening a scoped queue"
    finally:
        bus.close()
