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


def test_routed_log_preserves_the_configured_default_policy():
    policy = QueuePolicy(max_deliver=2, ack_wait_ms=1234, backoff_ms=(0, 50))
    bus = open_bus(BusConfig(scoped=True, queue=policy))
    try:
        consumer = bus.events.consumer("configured", "swarm.>")
        assert bus.events.consumer("configured", "swarm.>", policy) is consumer
        assert consumer._policy == policy
        dead = bus.dead.consumer("dead-configured", "swarm.>")
        assert bus.dead.consumer("dead-configured", "swarm.>", policy) is dead
        assert dead._policy == policy
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


def test_aggregate_fetch_rotates_sources_with_sustained_backlog():
    calls = []

    def log(index):
        def fetch(batch, timeout_ms):
            calls.append(index)
            return [index] * batch

        return SimpleNamespace(consumer=lambda *_: SimpleNamespace(fetch=fetch))

    logs = [log(index) for index in range(3)]
    consumer = scope.MergedConsumer(lambda: logs, "durable", "swarm.>", None)
    assert [consumer.fetch(2, 100) for _ in range(6)] == [[0, 0], [1, 1], [2, 2]] * 2
    assert calls == [0, 1, 2, 0, 1, 2]


def test_scoped_adapters_forward_owned_work_and_explicit_policy():
    from unittest.mock import Mock

    policy = QueuePolicy(max_deliver=2)
    event = Envelope("id", "ready", "rig", "task", "work", 1, 0, "author", {})
    log = Mock()
    scoped = scope.ScopedLog(log, "rig", "events")
    assert scoped.publish("swarm.rig.ready", event) is log.publish.return_value
    log.publish.assert_called_once_with("swarm.rig.events.ready", event)
    assert scoped.consumer("durable", "swarm.rig.ready", policy) is log.consumer.return_value
    log.consumer.assert_called_once_with("durable", "swarm.rig.events.ready", policy)
    queue = Mock()
    owned = scope.ScopedQueue(queue, "rig", policy)
    assert owned.offer(event) is queue.offer.return_value
    queue.offer.assert_called_once_with(event)
    assert owned.take(123) is queue.take.return_value
    queue.take.assert_called_once_with(123)
    assert scope.scoped_subject("rig", "dead", "dead.>") == "swarm.rig.dead.>"


def test_aggregate_reuses_consumers_and_preserves_budget_and_batch(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(scope, "monotonic", lambda: 0.0)
    policy = QueuePolicy(max_deliver=2)
    first, second = Mock(), Mock()
    first.consumer.return_value.fetch.return_value = ["one"]
    second.consumer.return_value.fetch.return_value = ["two"]
    routed = scope.RoutedLog(lambda _: first, lambda: [first, second], policy)
    consumer = routed.consumer("durable", "swarm.>")
    assert consumer.fetch(3, 2000) == ["one", "two"]
    assert consumer.fetch(3, 2000) == ["one", "two"]
    first.consumer.assert_called_once_with("durable", "swarm.>", policy)
    second.consumer.assert_called_once_with("durable", "swarm.>", policy)
    first.consumer.return_value.fetch.assert_called_with(3, 1000)
    second.consumer.return_value.fetch.assert_called_with(2, 1000)
    with pytest.raises(ValueError) as caught:
        routed.consumer("durable", "swarm.other.>")
    assert str(caught.value) == "durable consumer 'durable' already has another filter or policy"
    first.consumer.return_value.fetch.reset_mock()
    second.consumer.return_value.fetch.reset_mock()
    consumer.fetch(3, 1)
    first.consumer.return_value.fetch.assert_called_once_with(3, 1)
    second.consumer.return_value.fetch.assert_called_once_with(2, 1)


def test_aggregate_deadline_uses_milliseconds(monkeypatch):
    from unittest.mock import Mock

    moments = iter([0.0, 0.9995])
    monkeypatch.setattr(scope, "monotonic", lambda: next(moments))
    log = Mock()
    log.consumer.return_value.fetch.return_value = []
    assert scope.MergedConsumer(lambda: [log], "durable", "swarm.>", None).fetch(1, 1000) == []
    log.consumer.return_value.fetch.assert_called_once_with(1, 1)
