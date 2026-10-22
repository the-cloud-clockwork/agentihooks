from dataclasses import replace

import pytest

from scripts.events.envelope import Envelope
from scripts.events.memory import MemoryBus
from scripts.hub import registry, runner

pytestmark = pytest.mark.unit


class Clock:
    now = 1000

    def __call__(self):
        return self.now


def source(version=1, swarm="crew", key="execution-one"):
    return Envelope(
        id=f"activity-{swarm}-{key}-{version}",
        type="execution.changed",
        swarm=swarm,
        entity="execution",
        key=key,
        version=version,
        time=1000,
        author="lifecycle",
        data={},
    )


@pytest.mark.parametrize("kind", ["idle", "stale", "handoff"])
def test_deadline_expires_at_its_boundary_without_another_input(kind):
    from scripts.hub.timers import Timers

    from scripts.events import deadlines

    bus, clock = MemoryBus(), Clock()
    timers = Timers(bus, clock)
    request = deadlines.schedule(source(), kind, 1100)
    timers.handle(request)
    output = bus.events.consumer("expiry", "swarm.*.deadline.expired")
    clock.now = 1099
    timers.fire()
    assert output.fetch(10, 0) == []
    clock.now = 1100
    timers.fire()
    deliveries = output.fetch(10, 0)
    assert [delivery.event for delivery in deliveries] == [deadlines.expired(request)]
    deliveries[0].ack()
    timers.fire()
    assert output.fetch(10, 0) == []


def test_replacement_cancellation_and_delayed_versions_suppress_obsolete_deadlines():
    from scripts.hub.timers import Timers

    from scripts.events import deadlines

    bus, clock = MemoryBus(), Clock()
    timers = Timers(bus, clock)
    first = deadlines.schedule(source(), "idle", 1100)
    latest = deadlines.schedule(source(2), "idle", 1200)
    timers.handle(first)
    timers.handle(latest)
    timers.handle(first)
    timers.handle(deadlines.cancel(source(3), "idle"))
    timers.handle(latest)
    clock.now = 1300
    timers.fire()
    assert bus.events.consumer("expiry", "swarm.*.deadline.expired").fetch(10, 0) == []


def test_restart_restores_pending_deadlines_and_keeps_swarms_and_executions_independent():
    from scripts.hub.timers import Timers

    from scripts.events import deadlines

    bus, clock = MemoryBus(), Clock()
    timers = Timers(bus, clock)
    requests = [
        deadlines.schedule(source(), "idle", 1100),
        deadlines.schedule(source(swarm="other"), "idle", 1100),
        deadlines.schedule(source(key="execution-two"), "handoff", 1100),
    ]
    for request in requests:
        timers.handle(request)
    timers.handle(deadlines.cancel(source(2), "idle"))
    clock.now = 1100
    Timers(bus, clock).fire()
    deliveries = bus.events.consumer("expiry", "swarm.*.deadline.expired").fetch(10, 0)
    assert {delivery.event.id for delivery in deliveries} == {deadlines.expired(r).id for r in requests[1:]}


def test_publication_retry_reuses_the_same_envelope_after_restart(monkeypatch):
    from scripts.hub.timers import Timers

    from scripts.events import deadlines

    bus, clock = MemoryBus(), Clock()
    timers = Timers(bus, clock)
    request = deadlines.schedule(source(), "stale", 1000)
    timers.handle(request)
    publish = bus.events.publish
    seen = []

    def lose_response(subject, event):
        seen.append((subject, event))
        publish(subject, event)
        raise OSError("lost publish response")

    monkeypatch.setattr(bus.events, "publish", lose_response)
    with pytest.raises(OSError, match="lost publish response"):
        timers.fire()
    monkeypatch.setattr(bus.events, "publish", publish)
    Timers(bus, clock).fire()
    deliveries = bus.events.consumer("expiry", "swarm.*.deadline.expired").fetch(10, 0)
    assert seen == [("swarm.crew.deadline.expired", deadlines.expired(request))]
    assert [delivery.event for delivery in deliveries] == [seen[0][1]]


def test_singleton_hub_services_deadlines_on_empty_passes_and_hands_over():
    from scripts.hub.timers import Timers

    from scripts.events import deadlines

    bus, clock = MemoryBus(), Clock()
    first, second = Timers(bus, clock), Timers(bus, clock)
    hubs = [
        runner.HubRunner(bus, registry.Registry([timer.reactor()]), holder, clock, 100)
        for timer, holder in [(first, "first"), (second, "second")]
    ]
    request = deadlines.schedule(source(), "handoff", 1100)
    bus.events.publish(deadlines.subject(request), request)
    assert hubs[0].run_once(timeout_ms=0) == {"timers": ["handled"]}
    clock.now = 1100
    hubs[0].run_once(timeout_ms=0)
    assert hubs[1].run_once(timeout_ms=0) == {"timers": ["standby"]}
    hubs[0].stop()
    hubs[1].run_once(timeout_ms=0)
    deliveries = bus.events.consumer("expiry", "swarm.*.deadline.expired").fetch(10, 0)
    assert [delivery.event for delivery in deliveries] == [deadlines.expired(request)]
    hubs[1].stop()


def test_production_registry_builds_the_singleton_timers_reactor():
    bus = MemoryBus()
    declared = list(registry.configured(bus, Clock()))
    timer = next(reactor for reactor in declared if reactor.name == "timers")
    assert timer.singleton is True
    assert timer.subject == "swarm.*.deadline.*"
    assert callable(timer.poll)


def test_expiry_events_do_not_reschedule_themselves():
    from scripts.hub.timers import Timers

    from scripts.events import deadlines

    bus, clock = MemoryBus(), Clock()
    timers = Timers(bus, clock)
    request = deadlines.schedule(source(), "idle", 1000)
    timers.handle(request)
    timers.fire()
    timers.handle(deadlines.expired(request))
    assert replace(request, type="deadline.expired") != request
    timers.fire()
    assert len(bus.events.consumer("expiry", "swarm.*.deadline.expired").fetch(10, 0)) == 1
