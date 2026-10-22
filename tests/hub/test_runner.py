import pytest

from scripts.events import memory
from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.hub import lease, registry, runner

pytestmark = pytest.mark.unit

SUBJECT = "swarm.s.task.t1"


class Clock:
    def __init__(self):
        self.now = 1_000

    def __call__(self) -> int:
        return self.now


class Recorder:
    def __init__(self, fail: int = 0):
        self.seen: list[tuple[str, int]] = []
        self.fail = fail

    def __call__(self, event: Envelope) -> None:
        if self.fail:
            self.fail -= 1
            raise RuntimeError("handler failed")
        self.seen.append((event.id, event.version))


def event(event_id: str, version: int, key: str = "t1", swarm: str = "s") -> Envelope:
    return Envelope(
        id=event_id,
        type="task.claimed",
        swarm=swarm,
        entity="task",
        key=key,
        version=version,
        time=0,
        author="a",
        data={},
    )


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def bus(clock):
    return memory.open_bus(BusConfig(), clock=clock)


def hub(bus, reactors, holder="a", clock=None):
    return runner.HubRunner(bus, registry.Registry(reactors), holder, clock=clock or Clock(), lease_ttl_ms=100)


def test_the_registry_names_one_durable_consumer_per_reactor():
    declared = registry.Registry(
        [
            registry.Reactor("lifecycle", "swarm.*.task.>", Recorder()),
            registry.Reactor("timers", "swarm.*.timer.>", Recorder()),
        ]
    )
    assert [registry.durable(reactor) for reactor in declared] == ["hub-lifecycle", "hub-timers"]


def test_the_registry_refuses_a_second_reactor_with_the_same_name():
    with pytest.raises(ValueError, match="reactor 'lifecycle' is declared twice"):
        registry.Registry(
            [
                registry.Reactor("lifecycle", "swarm.>", Recorder()),
                registry.Reactor("lifecycle", "swarm.*.task.>", Recorder()),
            ]
        )


@pytest.mark.parametrize("name", ["", "Life", "life cycle", "life.cycle", "-life"])
def test_a_reactor_name_must_be_a_lowercase_token(name):
    with pytest.raises(ValueError, match="reactor name"):
        registry.Reactor(name, "swarm.>", Recorder())


def test_the_default_registry_declares_reactors():
    assert isinstance(registry.REACTORS, registry.Registry)


def test_a_reactor_handles_each_event_on_its_subject_once(bus):
    handler = Recorder()
    hosted = hub(bus, [registry.Reactor("lifecycle", "swarm.*.task.>", handler)])
    bus.events.publish(SUBJECT, event("e1", 1))
    bus.events.publish("swarm.s.hive.h1", event("e2", 1))
    assert hosted.run_once(batch=10, timeout_ms=0) == {"lifecycle": ["handled"]}
    assert hosted.run_once(batch=10, timeout_ms=0) == {"lifecycle": []}
    assert handler.seen == [("e1", 1)]


def test_a_repeated_or_older_entity_version_is_acknowledged_without_the_handler(bus):
    handler = Recorder()
    hosted = hub(bus, [registry.Reactor("lifecycle", "swarm.*.task.>", handler)])
    for event_id, version in (("e1", 2), ("e2", 2), ("e3", 1), ("e4", 3)):
        bus.events.publish(SUBJECT, event(event_id, version))
    assert hosted.run_once(batch=10, timeout_ms=0) == {"lifecycle": ["handled", "skipped", "skipped", "handled"]}
    assert handler.seen == [("e1", 2), ("e4", 3)]


def test_versions_are_tracked_per_swarm_entity_and_key(bus):
    handler = Recorder()
    hosted = hub(bus, [registry.Reactor("lifecycle", "swarm.*.task.>", handler)])
    bus.events.publish(SUBJECT, event("e1", 4))
    bus.events.publish(SUBJECT, event("e2", 1, key="t2"))
    bus.events.publish("swarm.u.task.t1", event("e3", 1, swarm="u"))
    assert hosted.run_once(batch=10, timeout_ms=0) == {"lifecycle": ["handled"] * 3}


def test_each_reactor_keeps_its_own_versions(bus):
    first, second = Recorder(), Recorder()
    hosted = hub(
        bus, [registry.Reactor("lifecycle", "swarm.*.task.>", first), registry.Reactor("page", "swarm.>", second)]
    )
    bus.events.publish(SUBJECT, event("e1", 1))
    assert hosted.run_once(batch=10, timeout_ms=0) == {"lifecycle": ["handled"], "page": ["handled"]}
    assert first.seen == second.seen == [("e1", 1)]


def test_two_replicas_share_the_durable_consumer_and_its_versions(bus):
    handler = Recorder()
    reactors = [registry.Reactor("lifecycle", "swarm.*.task.>", handler)]
    one, two = hub(bus, reactors, "a"), hub(bus, reactors, "b")
    bus.events.publish(SUBJECT, event("e1", 1))
    assert one.run_once(batch=10, timeout_ms=0) == {"lifecycle": ["handled"]}
    bus.events.publish(SUBJECT, event("e2", 1))
    assert two.run_once(batch=10, timeout_ms=0) == {"lifecycle": ["skipped"]}
    assert handler.seen == [("e1", 1)]


def test_a_failed_handler_is_redelivered_and_records_no_version(bus):
    handler = Recorder(fail=1)
    hosted = hub(bus, [registry.Reactor("lifecycle", "swarm.*.task.>", handler)])
    bus.events.publish(SUBJECT, event("e1", 1))
    assert hosted.run_once(batch=10, timeout_ms=0) == {"lifecycle": ["failed"]}
    assert hosted.run_once(batch=10, timeout_ms=0) == {"lifecycle": ["handled"]}
    assert handler.seen == [("e1", 1)]


def test_a_singleton_reactor_runs_only_on_the_lease_holder(bus, clock):
    handler = Recorder()
    reactors = [registry.Reactor("timers", "swarm.*.task.>", handler, singleton=True)]
    one, two = hub(bus, reactors, "a", clock), hub(bus, reactors, "b", clock)
    bus.events.publish(SUBJECT, event("e1", 1))
    assert two.run_once(batch=10, timeout_ms=0) == {"timers": ["handled"]}
    bus.events.publish(SUBJECT, event("e2", 2))
    assert one.run_once(batch=10, timeout_ms=0) == {"timers": ["standby"]}
    assert two.run_once(batch=10, timeout_ms=0) == {"timers": ["handled"]}
    assert lease.read(bus.state(lease.BUCKET), "reactor.timers").holder == "b"


def test_a_stopped_holder_hands_the_singleton_to_the_other_replica(bus, clock):
    reactors = [registry.Reactor("timers", "swarm.*.task.>", Recorder(), singleton=True)]
    one, two = hub(bus, reactors, "a", clock), hub(bus, reactors, "b", clock)
    one.run_once(batch=10, timeout_ms=0)
    assert two.run_once(batch=10, timeout_ms=0) == {"timers": ["standby"]}
    one.stop()
    bus.events.publish(SUBJECT, event("e1", 1))
    assert two.run_once(batch=10, timeout_ms=0) == {"timers": ["handled"]}


def test_a_lost_singleton_lease_is_taken_over_after_its_expiry(bus, clock):
    reactors = [registry.Reactor("timers", "swarm.*.task.>", Recorder(), singleton=True)]
    one, two = hub(bus, reactors, "a", clock), hub(bus, reactors, "b", clock)
    one.run_once(batch=10, timeout_ms=0)
    clock.now += 100
    assert two.run_once(batch=10, timeout_ms=0) == {"timers": []}
    assert one.run_once(batch=10, timeout_ms=0) == {"timers": ["standby"]}


def test_the_hub_command_runs_the_registry_once_on_the_configured_bus(monkeypatch, capsys):
    handler = Recorder()
    declared = registry.Registry([registry.Reactor("lifecycle", "swarm.*.task.>", handler)])
    opened = memory.open_bus(BusConfig())
    opened.events.publish(SUBJECT, event("e1", 1))
    monkeypatch.setattr(registry, "REACTORS", declared)
    monkeypatch.setattr(runner, "open_bus", lambda config: opened)
    monkeypatch.setenv("HOSTNAME", "hub-0")
    assert runner.main(["run", "--once"]) == 0
    assert handler.seen == [("e1", 1)]
    assert "lifecycle: handled" in capsys.readouterr().out
