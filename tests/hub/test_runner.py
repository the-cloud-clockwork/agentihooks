import hashlib
import signal

import pytest

from scripts import cli_delegates
from scripts.events import memory
from scripts.events.bus import StateEntry
from scripts.events.config import BusConfig, QueuePolicy
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


@pytest.mark.parametrize("retry_ms", [[10], (-1,), (1.5,), (True,)])
def test_reactor_retry_delays_must_be_non_negative_integers(retry_ms):
    with pytest.raises(ValueError) as raised:
        registry.Reactor("life", "swarm.>", Recorder(), retry_ms=retry_ms)
    assert str(raised.value) == "reactor 'life' retry delays must be a tuple of non negative integers"
    assert registry.Reactor("life", "swarm.>", Recorder(), retry_ms=(0, 5)).retry_ms == (0, 5)


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


def test_a_failed_handler_is_redelivered_and_records_no_version(bus, clock):
    handler = Recorder(fail=1)
    hosted = hub(bus, [registry.Reactor("lifecycle", "swarm.*.task.>", handler)])
    bus.events.publish(SUBJECT, event("e1", 1))
    assert hosted.run_once(batch=10, timeout_ms=0) == {"lifecycle": ["failed"]}
    clock.now += registry.RETRY_MS[0]
    assert hosted.run_once(batch=10, timeout_ms=0) == {"lifecycle": ["handled"]}
    assert handler.seen == [("e1", 1)]


def test_a_failed_handler_waits_out_its_reactor_backoff(bus, clock):
    hosted = hub(bus, [registry.Reactor("lifecycle", "swarm.*.task.>", Recorder(fail=2))])
    bus.events.publish(SUBJECT, event("e1", 1))
    first, second = registry.RETRY_MS[:2]
    assert hosted.run_once(batch=10, timeout_ms=0) == {"lifecycle": ["failed"]}
    clock.now += first - 1
    assert hosted.run_once(batch=10, timeout_ms=0) == {"lifecycle": []}
    clock.now += 1
    assert hosted.run_once(batch=10, timeout_ms=0) == {"lifecycle": ["failed"]}
    clock.now += second - 1
    assert hosted.run_once(batch=10, timeout_ms=0) == {"lifecycle": []}
    clock.now += 1
    assert hosted.run_once(batch=10, timeout_ms=0) == {"lifecycle": ["handled"]}


def test_a_reactor_outlasts_the_bus_attempt_cap_by_default(bus, clock):
    failures = BusConfig().queue.max_deliver + 3
    handler = Recorder(fail=failures)
    hosted = hub(bus, [registry.Reactor("lifecycle", "swarm.*.task.>", handler)])
    bus.events.publish(SUBJECT, event("e1", 1))
    outcomes = []
    for _ in range(failures + 1):
        outcomes += hosted.run_once(batch=10, timeout_ms=0)["lifecycle"]
        clock.now += max(registry.RETRY_MS)
    assert outcomes == ["failed"] * failures + ["handled"]
    assert handler.seen == [("e1", 1)]
    assert registry.REACTOR_POLICY.max_deliver > BusConfig().queue.max_deliver
    default = registry.Reactor("lifecycle", "swarm.>", Recorder())
    assert (default.policy, default.retry_ms) == (registry.REACTOR_POLICY, registry.RETRY_MS)
    assert registry.REACTOR_POLICY.backoff_ms == ()


def test_an_exhausted_reactor_event_is_dead_lettered_with_its_reactor_named(bus, clock):
    reactor = registry.Reactor(
        "lifecycle", "swarm.*.task.>", Recorder(fail=99), policy=QueuePolicy(max_deliver=2), retry_ms=(10,)
    )
    hosted = hub(bus, [reactor])
    original = event("e1", 1)
    bus.events.publish(SUBJECT, original)
    for _ in range(4):
        hosted.run_once(batch=10, timeout_ms=0)
        clock.now += 10
    letters = bus.dead.consumer("dead-reader", "dead.>").fetch(10, 0)
    assert [letter.event.data for letter in letters] == [
        {"event": original.to_dict(), "attempts": 2, "source": "events.hub-lifecycle"}
    ]


def test_the_retry_delay_follows_the_schedule_and_holds_its_last_step():
    assert [runner.retry_delay((10, 20), attempt) for attempt in (1, 2, 3, 4)] == [10, 20, 20, 20]
    assert runner.retry_delay((), 3) == 0


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


def test_a_singleton_lease_lost_inside_a_batch_hands_the_rest_back(bus, clock):
    handler = Recorder()

    def slow(event: Envelope) -> None:
        handler(event)
        clock.now += 100
        lease.Lease(bus.state(lease.BUCKET), "reactor.timers", "b", ttl_ms=100, clock=clock).acquire()

    one = hub(bus, [registry.Reactor("timers", "swarm.*.task.>", slow, singleton=True)], "a", clock)
    two = hub(bus, [registry.Reactor("timers", "swarm.*.task.>", handler, singleton=True)], "b", clock)
    bus.events.publish(SUBJECT, event("e1", 1))
    bus.events.publish(SUBJECT, event("e2", 2))
    bus.events.publish(SUBJECT, event("e3", 3))
    assert one.run_once(batch=10, timeout_ms=0) == {"timers": ["handled", "standby", "standby"]}
    assert two.run_once(batch=10, timeout_ms=0) == {"timers": ["handled", "handled"]}
    assert handler.seen == [("e1", 1), ("e2", 2), ("e3", 3)]


def test_the_hub_command_runs_the_registry_once_on_the_configured_bus(monkeypatch, capsys):
    handler = Recorder()
    declared = registry.Registry([registry.Reactor("lifecycle", "swarm.*.task.>", handler, singleton=True)])
    opened = memory.open_bus(BusConfig())
    opened.events.publish(SUBJECT, event("e1", 1))

    def configured(bus):
        assert bus is opened
        return declared

    monkeypatch.setattr(registry, "configured", configured)
    monkeypatch.setattr(runner, "open_bus", lambda config: opened if config == BusConfig(backend="memory") else None)
    monkeypatch.setenv("AGENTIHOOKS_BUS", "memory")
    monkeypatch.delenv("AGENTIHOOKS_BUS_URL", raising=False)
    monkeypatch.setenv("HOSTNAME", "hub-0")
    passes = []
    run_once = runner.HubRunner.run_once

    def once(self):
        if passes:
            raise Stop
        passes.append(run_once(self))
        return passes[-1]

    monkeypatch.setattr(runner.HubRunner, "run_once", once)
    previous = signal.getsignal(signal.SIGTERM)
    leases = opened.state(lease.BUCKET)
    assert runner.main(["run", "--once"]) == 0
    assert handler.seen == [("e1", 1)]
    assert capsys.readouterr().out == "lifecycle: handled\n"
    assert lease.read(leases, "reactor.lifecycle") == lease.LeaseState("hub-0", 0)
    assert signal.getsignal(signal.SIGTERM) is previous


def test_the_hub_command_refuses_an_unknown_action(capsys):
    with pytest.raises(SystemExit):
        runner.main(["stop"])
    assert "usage: agentihooks hub" in capsys.readouterr().err


def test_sigterm_ends_the_hub_with_143_and_releases_its_leases(monkeypatch):
    def terminate(event: Envelope) -> None:
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        raise Stop

    declared = registry.Registry([registry.Reactor("timers", "swarm.*.task.>", terminate, singleton=True)])
    opened = memory.open_bus(BusConfig())
    opened.events.publish(SUBJECT, event("e1", 1))

    def configured(bus):
        assert bus is opened
        return declared

    monkeypatch.setattr(registry, "configured", configured)
    monkeypatch.setattr(runner, "open_bus", lambda config: opened)
    monkeypatch.setenv("HOSTNAME", "hub-1")
    leases = opened.state(lease.BUCKET)
    with pytest.raises(SystemExit) as stopped:
        runner.main(["run"])
    assert stopped.value.code == 143
    assert lease.read(leases, "reactor.timers") == lease.LeaseState("hub-1", 0)


class Stop(BaseException):
    pass


def test_the_hub_loop_idles_only_after_a_pass_with_every_reactor_on_standby(monkeypatch):
    passes = iter([{"page": ["handled"]}, {"page": ["standby"]}, {"page": []}])

    def run_once(self):
        try:
            return next(passes)
        except StopIteration:
            raise Stop from None

    slept = []
    monkeypatch.setattr(runner.HubRunner, "run_once", run_once)
    monkeypatch.setattr(runner, "scheduled_wait", slept.append)
    monkeypatch.setattr(runner, "open_bus", lambda config: memory.open_bus(BusConfig()))
    with pytest.raises(Stop):
        runner.main(["run"])
    assert slept == [runner.IDLE_S]


def test_a_failed_handler_names_its_reactor_and_event_on_stderr(bus, capsys):
    hosted = hub(bus, [registry.Reactor("lifecycle", "swarm.*.task.>", Recorder(fail=1))])
    bus.events.publish(SUBJECT, event("e1", 1))
    hosted.run_once(batch=10, timeout_ms=0)
    err = capsys.readouterr().err
    assert err.startswith("hub: reactor lifecycle failed on event e1\n")
    assert "RuntimeError: handler failed" in err


def test_the_handled_key_names_reactor_entity_and_a_digest_of_swarm_and_key():
    reactor = registry.Reactor("lifecycle", "swarm.>", Recorder())
    digest = hashlib.sha256(b"s\0t1").hexdigest()
    assert runner.handled_key(reactor, event("e1", 1)) == f"lifecycle.task.{digest}"


def test_handled_versions_live_in_the_versions_bucket(bus):
    reactor = registry.Reactor("lifecycle", "swarm.*.task.>", Recorder())
    hosted = hub(bus, [reactor])
    bus.events.publish(SUBJECT, event("e1", 3))
    hosted.run_once(batch=10, timeout_ms=0)
    assert bus.state(runner.VERSIONS).get(runner.handled_key(reactor, event("e1", 3))).value == b"3"


def test_record_raises_the_version_and_never_lowers_or_rewrites_it(bus):
    store = bus.state(runner.VERSIONS)
    runner.record(store, "k", 2)
    runner.record(store, "k", 5)
    revision = store.get("k").revision
    runner.record(store, "k", 5)
    runner.record(store, "k", 4)
    assert store.get("k") == StateEntry("k", b"5", revision)


class Contended:
    def __init__(self, store, rival: int):
        self.store = store
        self.rival = rival

    def get(self, key):
        entry = self.store.get(key)
        if self.rival:
            version, self.rival = self.rival, 0
            runner.record(self.store, key, version)
        return entry

    def create(self, key, value):
        return self.store.create(key, value)

    def update(self, key, value, revision):
        return self.store.update(key, value, revision)


@pytest.mark.parametrize(("rival", "kept"), [(3, b"7"), (9, b"9")])
def test_record_retries_a_lost_compare_and_set_and_keeps_the_higher_version(bus, rival, kept):
    store = bus.state(runner.VERSIONS)
    runner.record(store, "k", 1)
    runner.record(Contended(store, rival), "k", 7)
    assert store.get("k").value == kept


def test_the_hub_command_is_a_delegated_cli():
    assert cli_delegates.delegated_cli(["hub", "run"]) is runner.main
