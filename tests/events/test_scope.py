from types import SimpleNamespace

import pytest

from scripts.events import scope
from scripts.events.config import BusConfig, QueuePolicy
from scripts.events.envelope import Envelope
from scripts.events.memory import open_bus

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("name", ["eng", "swarm.rig.ready.eng"])
def test_queue_names_select_only_a_lane_in_the_bound_swarm(name):
    assert scope.queue_lane("rig", name) == "eng"


@pytest.mark.parametrize("name", ["swarm.other.ready.eng", "anything", "swarm.rig.ready.unknown"])
def test_queue_names_refuse_foreign_or_unknown_work(name):
    with pytest.raises(ValueError, match="does not belong"):
        scope.queue_lane("rig", name)


def test_scoped_log_wire_subjects_keep_the_owner_and_existing_filters():
    assert scope.scoped_subject("rig", "events", "swarm.*.execution.started") == "swarm.rig.events.execution.started"
    assert scope.scoped_subject("rig", "events", "swarm.>") == "swarm.rig.events.>"
    assert scope.scoped_subject("rig", "dead", "dead.rig") == "swarm.rig.dead.event"
    assert scope.scoped_subject("rig", "dead", "dead.rig.letter") == "swarm.rig.dead.letter"
    with pytest.raises(ValueError, match="does not belong"):
        scope.scoped_subject("rig", "events", "swarm.other.task.ready")


def test_scoped_queue_refuses_an_envelope_owned_by_another_swarm():
    bus = open_bus(BusConfig(scoped=True))
    try:
        foreign = Envelope("id", "task.ready", "other", "task", "work", 1, 0, "author", {})
        with pytest.raises(ValueError, match="does not match"):
            bus.for_swarm("rig").queue("eng", QueuePolicy()).offer(foreign)
    finally:
        bus.close()


def test_routed_log_keeps_the_event_log_keyword_interface_and_default_policy():
    bus = open_bus(BusConfig(scoped=True))
    try:
        event = Envelope("id", "task.ready", "rig", "task", "work", 1, 0, "author", {})
        assert bus.events.publish(subject="swarm.rig.task.ready", event=event)
        consumer = bus.events.consumer("durable", subject="swarm.>")
        assert bus.events.consumer("durable", subject="swarm.>", policy=QueuePolicy()) is consumer
    finally:
        bus.close()


def test_aggregate_fetch_honors_the_budget_and_rotates_empty_sources(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(scope, "monotonic", lambda: clock.now, raising=False)
    calls = []

    def log(index):
        def fetch(batch, timeout_ms):
            calls.append(index)
            clock.now += 0.01
            return []

        return SimpleNamespace(consumer=lambda *_: SimpleNamespace(fetch=fetch))

    logs = [log(index) for index in range(5)]
    consumer = scope.MergedConsumer(lambda: logs, "durable", "swarm.>", None)
    assert consumer.fetch(10, 20) == []
    assert calls == [0, 1]
    assert consumer.fetch(10, 20) == []
    assert calls == [0, 1, 2, 3]
