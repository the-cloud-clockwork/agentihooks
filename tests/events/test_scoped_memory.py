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
