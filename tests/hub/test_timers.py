from dataclasses import replace

import pytest

from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.events.memory import open_bus
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
    from scripts.events import deadlines
    from scripts.hub import timers as timer_module

    bus, clock = open_bus(BusConfig()), Clock()
    timers = timer_module.Timers(bus, clock)
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
    from scripts.events import deadlines
    from scripts.hub import timers as timer_module

    bus, clock = open_bus(BusConfig()), Clock()
    timers = timer_module.Timers(bus, clock)
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
    from scripts.events import deadlines
    from scripts.hub import timers as timer_module

    bus, clock = open_bus(BusConfig()), Clock()
    timers = timer_module.Timers(bus, clock)
    requests = [
        deadlines.schedule(source(), "idle", 1100),
        deadlines.schedule(source(swarm="other"), "idle", 1100),
        deadlines.schedule(source(key="execution-two"), "handoff", 1100),
    ]
    for request in requests:
        timers.handle(request)
    timers.handle(deadlines.cancel(source(2), "idle"))
    clock.now = 1100
    timer_module.Timers(bus, clock).fire()
    deliveries = bus.events.consumer("expiry", "swarm.*.deadline.expired").fetch(10, 0)
    assert {delivery.event.id for delivery in deliveries} == {deadlines.expired(r).id for r in requests[1:]}


def test_publication_retry_reuses_the_same_envelope_after_restart(monkeypatch):
    from scripts.events import deadlines
    from scripts.hub import timers as timer_module

    bus, clock = open_bus(BusConfig()), Clock()
    timers = timer_module.Timers(bus, clock)
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
    timer_module.Timers(bus, clock).fire()
    deliveries = bus.events.consumer("expiry", "swarm.*.deadline.expired").fetch(10, 0)
    assert seen == [("swarm.crew.deadline.expired", deadlines.expired(request))]
    assert [delivery.event for delivery in deliveries] == [seen[0][1]]


def test_singleton_hub_services_deadlines_on_empty_passes_and_hands_over():
    from scripts.events import deadlines
    from scripts.hub import timers as timer_module

    bus, clock = open_bus(BusConfig()), Clock()
    first, second = timer_module.Timers(bus, clock), timer_module.Timers(bus, clock)
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
    bus = open_bus(BusConfig())
    declared = list(registry.configured(bus, Clock()))
    timer = next(reactor for reactor in declared if reactor.name == "timers")
    assert timer.singleton is True
    assert timer.subject == "swarm.*.deadline.*"
    assert callable(timer.poll)


def test_expiry_events_do_not_reschedule_themselves():
    from scripts.events import deadlines
    from scripts.hub import timers as timer_module

    bus, clock = open_bus(BusConfig()), Clock()
    timers = timer_module.Timers(bus, clock)
    request = deadlines.schedule(source(), "idle", 1000)
    timers.handle(request)
    timers.fire()
    timers.handle(deadlines.expired(request))
    assert replace(request, type="deadline.expired") != request
    timers.fire()
    assert len(bus.events.consumer("expiry", "swarm.*.deadline.expired").fetch(10, 0)) == 1


def test_hub_command_registers_and_fires_the_default_timer(monkeypatch):
    from scripts.events import deadlines

    bus = open_bus(BusConfig())
    request = deadlines.schedule(source(), "idle", 0)
    bus.events.publish(deadlines.subject(request), request)
    monkeypatch.setattr(runner, "open_bus", lambda config: bus)
    assert runner.main(["run", "--once"]) == 0
    deliveries = bus.events.consumer("expiry", "swarm.*.deadline.expired").fetch(10, 0)
    assert [delivery.event for delivery in deliveries] == [deadlines.expired(request)]


def test_a_lost_lease_stops_due_publication_until_the_next_holder():
    from scripts.events import deadlines
    from scripts.hub import timers as timer_module

    bus, clock = open_bus(BusConfig()), Clock()
    timer = timer_module.Timers(bus, clock)
    request = deadlines.schedule(source(), "idle", 1000)
    timer.handle(request)
    output = bus.events.consumer("expiry", "swarm.*.deadline.expired")
    timer.fire(lambda: False)
    assert output.fetch(10, 0) == []
    timer_module.Timers(bus, clock).fire()
    assert [delivery.event for delivery in output.fetch(10, 0)] == [deadlines.expired(request)]


def test_equal_versions_and_completed_versions_cannot_rearm_a_timer():
    from scripts.events import deadlines
    from scripts.hub import timers as timer_module

    bus, clock = open_bus(BusConfig()), Clock()
    timer = timer_module.Timers(bus, clock)
    first = deadlines.schedule(source(), "idle", 1000)
    timer.handle(first)
    timer.handle(deadlines.schedule(source(), "idle", 2000))
    timer.fire()
    timer.handle(first)
    timer.fire()
    second = deadlines.schedule(source(2), "idle", 1000)
    timer.handle(second)
    timer.fire()
    events = bus.events.consumer("expiry", "swarm.*.deadline.expired").fetch(10, 0)
    assert [delivery.event for delivery in events] == [deadlines.expired(first), deadlines.expired(second)]


def test_cancelling_an_unseen_timer_is_a_durable_tombstone():
    from scripts.events import deadlines
    from scripts.hub import timers as timer_module

    bus, clock = open_bus(BusConfig()), Clock()
    timer = timer_module.Timers(bus, clock)
    timer.fire()
    timer.handle(deadlines.cancel(source(2), "handoff"))
    timer.handle(deadlines.schedule(source(), "handoff", 0))
    timer_module.Timers(bus, clock).fire()
    assert bus.events.consumer("expiry", "swarm.*.deadline.expired").fetch(10, 0) == []


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("permanent", [False, True])
def test_timer_state_compare_and_swap_retries_or_reports_contention(monkeypatch, operation, permanent):
    from scripts.events import deadlines
    from scripts.events.bus import StateConflict
    from scripts.hub import timers as timer_module

    bus, clock = open_bus(BusConfig()), Clock()
    timer = timer_module.Timers(bus, clock)
    if operation == "update":
        timer.handle(deadlines.schedule(source(), "idle", 2000))
    store = bus.state(timer_module.BUCKET)
    original = getattr(store, operation)
    calls = []

    def conflict(*args):
        calls.append(args)
        assert args[0] == "deadlines"
        if permanent or len(calls) == 1:
            raise StateConflict("competing writer")
        return original(*args)

    monkeypatch.setattr(store, operation, conflict)
    event = deadlines.schedule(source(2), "idle", 1000)
    if permanent:
        with pytest.raises(StateConflict) as error:
            timer.handle(event)
        assert str(error.value) == "deadline index remained contended"
        assert len(calls) == 8
    else:
        timer.handle(event)
        timer.fire()
        assert len(calls) == (2 if operation == "create" else 3)
        events = bus.events.consumer("expiry", "swarm.*.deadline.expired").fetch(10, 0)
        assert [delivery.event for delivery in events] == [deadlines.expired(event)]


def test_hub_reports_failed_timer_callback_and_retries_without_losing_the_deadline(monkeypatch, capsys):
    from scripts.events import deadlines
    from scripts.hub import timers as timer_module

    bus, clock = open_bus(BusConfig()), Clock()
    timer = timer_module.Timers(bus, clock)
    hub = runner.HubRunner(bus, registry.Registry([timer.reactor()]), "holder", clock)
    request = deadlines.schedule(source(), "stale", 1000)
    bus.events.publish(deadlines.subject(request), request)
    publish = bus.events.publish

    def fail(subject, event):
        assert subject == "swarm.crew.deadline.expired"
        assert event == deadlines.expired(request)
        raise OSError("publication unavailable")

    monkeypatch.setattr(bus.events, "publish", fail)
    assert hub.run_once(timeout_ms=0) == {"timers": ["handled", "failed"]}
    assert "hub: reactor timers timer callback failed" in capsys.readouterr().err
    monkeypatch.setattr(bus.events, "publish", publish)
    hub.run_once(timeout_ms=0)
    events = bus.events.consumer("expiry", "swarm.*.deadline.expired").fetch(10, 0)
    assert [delivery.event for delivery in events] == [deadlines.expired(request)]
    hub.stop()


def test_full_input_batches_do_not_starve_a_due_timer():
    from scripts.events import deadlines
    from scripts.hub import timers as timer_module

    bus, clock = open_bus(BusConfig()), Clock()
    timer = timer_module.Timers(bus, clock)
    hub = runner.HubRunner(bus, registry.Registry([timer.reactor()]), "holder", clock)
    first = deadlines.schedule(source(), "idle", 1000)
    later = deadlines.schedule(source(2), "handoff", 2000)
    for event in (first, later):
        bus.events.publish(deadlines.subject(event), event)
    assert hub.run_once(batch=1, timeout_ms=0) == {"timers": ["handled"]}
    events = bus.events.consumer("expiry", "swarm.*.deadline.expired").fetch(10, 0)
    assert [delivery.event for delivery in events] == [deadlines.expired(first)]
    hub.stop()


def test_reacquiring_a_lease_refreshes_cancelled_cached_deadlines():
    from scripts.events import deadlines
    from scripts.hub import timers as timer_module

    bus, clock = open_bus(BusConfig()), Clock()
    first, successor = timer_module.Timers(bus, clock), timer_module.Timers(bus, clock)
    request = deadlines.schedule(source(), "idle", 1000)
    first.handle(request)

    def takeover():
        successor.handle(deadlines.cancel(source(2), "idle"))
        return True

    first.fire(takeover)
    assert bus.events.consumer("expiry", "swarm.*.deadline.expired").fetch(10, 0) == []


def test_configured_registry_uses_its_clock_for_deadline_boundaries():
    from scripts.events import deadlines

    bus, clock = open_bus(BusConfig()), Clock()
    reactor = next(r for r in registry.configured(bus, clock) if r.name == "timers")
    request = deadlines.schedule(source(), "idle", 1100)
    reactor.handler(request)
    output = bus.events.consumer("expiry", "swarm.*.deadline.expired")
    reactor.poll(lambda: True)
    assert output.fetch(10, 0) == []
    clock.now = 1100
    reactor.poll(lambda: True)
    assert [delivery.event for delivery in output.fetch(10, 0)] == [deadlines.expired(request)]


def test_timer_callbacks_receive_the_current_lease_guard(monkeypatch):
    from scripts.events import deadlines

    bus, clock = open_bus(BusConfig()), Clock()
    declared = registry.configured(bus, clock)
    hub = runner.HubRunner(bus, declared, "holder", clock)
    request = deadlines.schedule(source(), "idle", 1000)
    bus.events.publish(deadlines.subject(request), request)
    held = hub._leases["timers"]
    decisions = iter([True, False])
    monkeypatch.setattr(held, "acquire", lambda: next(decisions))
    assert hub.run_once(timeout_ms=0) == {"timers": ["handled"]}
    assert bus.events.consumer("expiry", "swarm.*.deadline.expired").fetch(10, 0) == []


def test_shared_reactor_timer_callbacks_receive_an_allowed_guard():
    bus = open_bus(BusConfig())
    decisions = []
    reactor = registry.Reactor(
        "shared", "swarm.*.task.*", lambda event: None, poll=lambda guard: decisions.append(guard())
    )
    hub = runner.HubRunner(bus, registry.Registry([reactor]), "holder")
    assert hub.run_once(timeout_ms=0) == {"shared": []}
    assert decisions == [True]


def test_expiry_batches_are_bounded_and_leave_the_rest_pending():
    from scripts.events import deadlines
    from scripts.hub import timers

    bus, clock = open_bus(BusConfig()), Clock()
    timer = timers.Timers(bus, clock)
    for index in range(40):
        timer.handle(deadlines.schedule(source(key=f"execution-{index}"), "idle", 1000))
    output = bus.events.consumer("expiry", "swarm.*.deadline.expired")
    timer.fire()
    first = output.fetch(100, 0)
    assert len(first) == 32
    for delivery in first:
        delivery.ack()
    timer.fire()
    assert len(output.fetch(100, 0)) == 8


def test_repeated_versions_do_not_rewrite_durable_timer_state():
    from scripts.events import deadlines
    from scripts.hub import timers

    bus, clock = open_bus(BusConfig()), Clock()
    timer = timers.Timers(bus, clock)
    request = deadlines.schedule(source(), "idle", 1200)
    timer.handle(request)
    store = bus.state("hub-timers")
    before = store.get("deadlines")
    timer.handle(request)
    assert store.get("deadlines") == before


def test_unchanged_timer_state_reuses_the_cached_heap(monkeypatch):
    from scripts.events import deadlines
    from scripts.hub import timers

    bus, clock = open_bus(BusConfig()), Clock()
    timer = timers.Timers(bus, clock)
    timer.handle(deadlines.schedule(source(), "idle", 1200))
    heapify = timers.heapq.heapify
    calls = []

    def record(items):
        calls.append(len(items))
        heapify(items)

    monkeypatch.setattr(timers.heapq, "heapify", record)
    timer.fire()
    timer.fire()
    assert calls == [1]
