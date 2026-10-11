import time
from types import SimpleNamespace

import pytest

from scripts.swarm import cli, controller, lease, timing
from scripts.swarm.store import RedisStore, SwarmConfig, SwarmError
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_delivery import FakeHerdr

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]


@pytest.fixture
def store():
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


@pytest.fixture
def clock(monkeypatch):
    now = [1000]
    monkeypatch.setattr(lease, "now_ms", lambda saved: now[0])
    return now


def test_renew_extends_the_live_lease_and_keeps_its_epoch(store, clock):
    held = lease.acquire(store, "sw", "home")
    clock[0] = 150000
    assert lease.renew(store, "sw", held) == lease.Lease("home", 1, 330000)
    assert lease.current(store, "sw") == lease.Lease("home", 1, 330000)
    assert store.redis.pttl(store.key("sw", "control-owner")) > 179000


def test_renew_refuses_a_lease_taken_by_another_owner(store, clock):
    held = lease.acquire(store, "sw", "home")
    clock[0] = held.expires_at
    stolen = lease.acquire(store, "sw", "other")
    with pytest.raises(SwarmError) as error:
        lease.renew(store, "sw", held)
    assert str(error.value) == "the controller lease is stale"
    assert lease.current(store, "sw") == stolen


def test_renew_refuses_the_same_owner_under_a_new_epoch(store, clock):
    held = lease.acquire(store, "sw", "home")
    assert lease.release(store, "sw", held)
    again = lease.acquire(store, "sw", "home")
    with pytest.raises(SwarmError):
        lease.renew(store, "sw", held)
    assert lease.current(store, "sw") == again


def test_renew_refuses_an_expired_lease(store, clock):
    held = lease.acquire(store, "sw", "home")
    clock[0] = held.expires_at
    with pytest.raises(SwarmError):
        lease.renew(store, "sw", held)
    assert lease.current(store, "sw") is None


def test_renew_retries_a_conflicting_transaction(store, clock, monkeypatch):
    held = lease.acquire(store, "sw", "home")
    pipeline, conflicts = store.redis.pipeline, [1]

    def interleaved():
        pipe = pipeline()
        execute = pipe.execute

        def commit():
            if conflicts:
                conflicts.pop()
                store.redis.pexpire(store.key("sw", "control-owner"), lease.ttl_ms())
            return execute()

        pipe.execute = commit
        return pipe

    monkeypatch.setattr(store.redis, "pipeline", interleaved)
    clock[0] = 2000
    assert lease.renew(store, "sw", held) == lease.Lease("home", 1, 182000)


def test_a_tick_slower_than_the_lease_keeps_writing(store, clock):
    store.create(SwarmConfig("sw", ".", 1, 0))
    held = lease.acquire(store, "sw", "home")
    writes = []
    ledger = controller.FencedLedger(
        store, "sw", held, SimpleNamespace(update_task=lambda *args: writes.append(args) or "accepted")
    )
    runtime = controller.FencedRuntime(
        store,
        "sw",
        held,
        SimpleNamespace(
            has_capacity=lambda saved: True,
            spawn=lambda *args: "placed",
            retire=lambda agent, homes=None: (agent, homes),
        ),
        "local",
    )
    for step in range(5):
        clock[0] += 120000
        assert ledger.update_task("sw", "t", {"step": step}) == "accepted"
    assert clock[0] - 1000 > lease.ttl_ms()
    assert runtime.has_capacity(store.config("sw")) is True
    clock[0] += 120000
    assert runtime.retire("one", homes=["home"]) == ("one", ["home"])
    clock[0] += 120000
    assert runtime.spawn(store.config("sw"), "eng", "one", {"id": "t"}) == "placed"
    assert len(writes) == 5
    assert lease.current(store, "sw") == lease.Lease("home", 1, clock[0] + lease.ttl_ms())


def test_a_tick_whose_lease_was_stolen_stops(store, clock):
    store.create(SwarmConfig("sw", ".", 1, 0))
    held = lease.acquire(store, "sw", "home")
    writes, spawned = [], []
    ledger = controller.FencedLedger(store, "sw", held, SimpleNamespace(update_task=lambda *args: writes.append(args)))
    runtime = controller.FencedRuntime(
        store, "sw", held, SimpleNamespace(spawn=lambda *args: spawned.append(args), retire=spawned.append), "local"
    )
    ledger.update_task("sw", "t", {"step": 0})
    clock[0] += lease.ttl_ms()
    stolen = lease.acquire(store, "sw", "other")
    for write in (
        lambda: ledger.update_task("sw", "t", {"step": 1}),
        lambda: runtime.spawn(store.config("sw"), "eng", "one", {"id": "t"}),
        lambda: runtime.retire("one"),
    ):
        with pytest.raises(SwarmError) as error:
            write()
        assert str(error.value) == "the controller lease is stale"
    assert len(writes) == 1
    assert spawned == []
    assert lease.current(store, "sw") == stolen


def test_run_tick_slower_than_the_lease_finishes(env, clock, monkeypatch):  # noqa: F811
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    started = clock[0]

    def slow(*args):
        clock[0] += 120000
        return []

    monkeypatch.setattr(cli.phase_planning, "planning_pass", slow)
    monkeypatch.setattr(cli.wake, "wake_pass", slow)
    cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    assert clock[0] - started > lease.ttl_ms()
    assert lease.current(store, "sw").epoch == 1
    assert store.redis.get(store.key("sw", "last-tick"))


def test_keep_tick_renews_the_lease_and_extends_the_tick_lock(store, clock):
    held = lease.acquire(store, "sw", "home")
    token = controller.take_tick_lock(store, "sw", held, 1000)
    clock[0] = 150000
    controller.keep_tick(store, "sw", held, token, 600000)
    assert store.redis.pttl(store.key("sw", "tick-lock")) > 1000
    assert store.redis.get(store.key("sw", "tick-lock")) == token
    assert lease.current(store, "sw") == lease.Lease("home", 1, 330000)


def test_keep_tick_refuses_a_tick_lock_it_no_longer_holds(store, clock):
    held = lease.acquire(store, "sw", "home")
    controller.take_tick_lock(store, "sw", held, 1000)
    store.redis.set(store.key("sw", "tick-lock"), "another tick", px=1000)
    with pytest.raises(SwarmError) as error:
        controller.keep_tick(store, "sw", held, "this tick", 600000)
    assert str(error.value) == "the tick lock is stale"
    assert store.redis.pttl(store.key("sw", "tick-lock")) <= 1000


def test_keep_tick_retries_a_conflicting_transaction(store, clock, monkeypatch):
    held = lease.acquire(store, "sw", "home")
    token = controller.take_tick_lock(store, "sw", held, 1000)
    pipeline, conflicts = store.redis.pipeline, [1]

    def interleaved():
        pipe = pipeline()
        execute = pipe.execute

        def commit():
            if conflicts and pipe.command_stack and pipe.command_stack[-1][0][0] == "PEXPIRE":
                conflicts.pop()
                store.redis.pexpire(store.key("sw", "tick-lock"), 1000)
            return execute()

        pipe.execute = commit
        return pipe

    monkeypatch.setattr(store.redis, "pipeline", interleaved)
    controller.keep_tick(store, "sw", held, token, 600000)
    assert conflicts == []
    assert store.redis.pttl(store.key("sw", "tick-lock")) > 1000


def test_a_step_runs_the_before_step_hook_first():
    calls = []
    keeping = timing.BEFORE_STEP.set(lambda: calls.append("keep"))
    try:
        assert timing.call(lambda: calls.append("step") or "done") == "done"
    finally:
        timing.BEFORE_STEP.reset(keeping)
    assert calls == ["keep", "step"]
    timing.call(lambda: calls.append("free"))
    assert calls == ["keep", "step", "free"]


def test_run_tick_with_slow_steps_that_never_touch_the_ledger_finishes(env, clock, monkeypatch):  # noqa: F811
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    started = clock[0]

    def slow(*args):
        clock[0] += 120000
        return []

    for module, name in ((cli.progress, "checks_pass"), (cli.waits, "end_pass"), (cli.quiet, "quiet_pass")):
        monkeypatch.setattr(module, name, slow)
    cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    assert clock[0] - started == 360000
    assert lease.current(store, "sw").epoch == 1
    assert not store.redis.exists(store.key("sw", "tick-lock"))
    assert timing.BEFORE_STEP.get() is None


def test_run_tick_stops_between_steps_when_another_controller_took_the_lease(env, clock, monkeypatch):  # noqa: F811
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    later = []

    def stolen(*args):
        clock[0] += lease.ttl_ms()
        lease.acquire(store, "sw", "other")
        return []

    monkeypatch.setattr(cli.progress, "checks_pass", stolen)
    monkeypatch.setattr(cli.waits, "end_pass", lambda *args: later.append(args) or [])
    with pytest.raises(SwarmError) as error:
        cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    assert str(error.value) == "the controller lease is stale"
    assert later == []
    assert lease.current(store, "sw").owner == "other"
    assert not store.redis.exists(store.key("sw", "tick-lock"))
    assert timing.BEFORE_STEP.get() is None


def test_default_tick_gives_a_three_minute_lease(store, clock, monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_CONTROLLER_TICK_SECONDS", raising=False)
    assert (lease.tick_ms(), lease.ttl_ms()) == (60000, 180000)
    held = lease.acquire(store, "sw", "home")
    assert held == lease.Lease("home", 1, 181000)
    assert 179000 < store.redis.pttl(store.key("sw", "control-owner")) <= 180000


def test_a_shorter_tick_shortens_the_lease_life_to_three_of_its_ticks(store, clock, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_CONTROLLER_TICK_SECONDS", "2.5")
    assert (lease.tick_ms(), lease.ttl_ms()) == (2500, 7500)
    held = lease.acquire(store, "sw", "home")
    assert held == lease.Lease("home", 1, 8500)
    assert 7000 < store.redis.pttl(store.key("sw", "control-owner")) <= 7500
    clock[0] = 5000
    assert lease.renew(store, "sw", held) == lease.Lease("home", 1, 12500)
    assert 7000 < store.redis.pttl(store.key("sw", "control-owner")) <= 7500


@pytest.mark.parametrize("seconds", ["abc", "0", "-5", "0.999", "nan", "inf"])
def test_a_tick_setting_below_one_second_or_not_a_number_is_refused(seconds, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_CONTROLLER_TICK_SECONDS", seconds)
    with pytest.raises(SwarmError) as error:
        lease.tick_ms()
    assert str(error.value) == "AGENTIHOOKS_CONTROLLER_TICK_SECONDS must be at least 1 second"


def test_a_one_second_tick_is_the_shortest_allowed(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_CONTROLLER_TICK_SECONDS", "1")
    assert lease.ttl_ms() == 3000


def test_the_controller_sleeps_one_configured_tick_between_passes(store, monkeypatch):
    import scripts.operator_env

    slept = []

    def sleep(seconds):
        slept.append(seconds)
        raise SwarmError("stopped after one pass")

    monkeypatch.setenv("AGENTIHOOKS_CONTROLLER_TICK_SECONDS", "4")
    monkeypatch.setattr(scripts.operator_env, "fill", lambda environ: [])
    monkeypatch.setattr(controller, "connect", lambda: store)
    monkeypatch.setattr(controller, "run_once", lambda saved: {})
    monkeypatch.setattr(controller.time, "sleep", sleep)
    assert controller.main(["run"]) == 1
    assert slept == [4.0]


def renewed(store, clock, wait_s=2.0):
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        held = lease.current(store, "sw")
        if held and held.expires_at == clock[0] + lease.ttl_ms():
            return True
        time.sleep(0.005)
    return False


class Stop:
    def __init__(self, clock, step, stops_after):
        self.clock, self.step, self.stops_after, self.waits = clock, step, stops_after, []

    def wait(self, seconds):
        self.waits.append(seconds)
        self.clock[0] += self.step
        return self.is_set()

    def is_set(self):
        return len(self.waits) > self.stops_after


def test_beat_renews_the_lease_once_a_tick_until_stopped(store, clock, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_CONTROLLER_TICK_SECONDS", "2.5")
    held = lease.acquire(store, "sw", "home")
    stop = Stop(clock, 5000, 3)
    assert lease.beat(store, "sw", held, stop) is None
    assert stop.waits == [2.5, 2.5, 2.5, 2.5]
    assert clock[0] == 21000
    assert lease.current(store, "sw") == lease.Lease("home", 1, 23500)


def test_beat_stops_once_the_lease_is_lost(store, clock, monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_CONTROLLER_TICK_SECONDS", raising=False)
    held = lease.acquire(store, "sw", "home")
    stop = Stop(clock, lease.ttl_ms(), 5)
    assert lease.beat(store, "sw", held, stop) is None
    assert stop.waits == [60.0]
    assert lease.current(store, "sw") is None


def test_kept_beats_only_while_its_block_runs(store, clock, monkeypatch):
    held, calls = lease.acquire(store, "sw", "home"), []

    def beat(*args):
        calls.append(args[:3])
        calls.append(args[3].wait(5))

    monkeypatch.setattr(lease, "beat", beat)
    with lease.kept(store, "sw", held):
        deadline = time.monotonic() + 5
        while not calls and time.monotonic() < deadline:
            time.sleep(0.005)
        assert calls == [(store, "sw", held)]
    assert calls == [(store, "sw", held), True]


def test_beat_keeps_beating_through_a_redis_error(store, clock, monkeypatch):
    from redis.exceptions import ConnectionError as RedisDown

    held, renewals = lease.acquire(store, "sw", "home"), []

    def renew(*args):
        renewals.append(args)
        if len(renewals) == 1:
            raise RedisDown("redis restarting")

    monkeypatch.setattr(lease, "renew", renew)
    assert lease.beat(store, "sw", held, Stop(clock, 0, 2)) is None
    assert renewals == [(store, "sw", held)] * 2


def test_a_fenced_launch_slower_than_the_lease_keeps_it(store, clock, monkeypatch):
    store.create(SwarmConfig("sw", ".", 1, 0))
    monkeypatch.delenv("AGENTIHOOKS_CONTROLLER_TICK_SECONDS", raising=False)
    monkeypatch.setattr(lease, "beat_s", lambda: 0.01)
    held, beats = lease.acquire(store, "sw", "home"), []

    def slow(*args):
        for _ in range(5):
            clock[0] += 120000
            beats.append(renewed(store, clock))
            if not beats[-1]:
                break
        return "placed"

    runtime = controller.FencedRuntime(store, "sw", held, SimpleNamespace(spawn=slow, relaunch=slow), "local")
    assert runtime.spawn(store.config("sw"), "eng", "one", {"id": "t"}) == "placed"
    assert runtime.relaunch("one") == "placed"
    assert beats == [True] * 10
    assert lease.current(store, "sw") == lease.Lease("home", 1, clock[0] + lease.ttl_ms())


def test_a_launch_slower_than_the_lease_keeps_the_intent_pass_in_the_same_tick(env, clock, monkeypatch):  # noqa: F811
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo", "--max-eng-agents", "1", "--max-ci-agents", "0")
    store.update("sw", state="running")
    monkeypatch.delenv("AGENTIHOOKS_CONTROLLER_TICK_SECONDS", raising=False)
    monkeypatch.setattr(lease, "beat_s", lambda: 0.01)
    spawn, events = rt.spawn, []

    def slow(config, lane, name, task):
        for _ in range(5):
            clock[0] += 120000
            events.append(renewed(store, clock))
            if not events[-1]:
                break
        return spawn(config, lane, name, task)

    class Check:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, doc):
            events.append("intent")
            return ["judged"]

    monkeypatch.setattr(rt, "spawn", slow)
    monkeypatch.setattr(cli.intent, "Check", Check)
    started = clock[0]
    actions = cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    assert clock[0] - started > lease.ttl_ms()
    assert events[-1] == "intent"
    assert events[:-1] and all(beat is True for beat in events[:-1])
    assert "judged" in actions
    assert lease.current(store, "sw").epoch == 1
    assert store.redis.get(store.key("sw", "last-tick"))


def test_a_slow_launch_that_never_starts_is_recorded_against_the_launch_not_the_tick(env, clock, monkeypatch):  # noqa: F811
    from scripts.swarm.tick import SpawnError

    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo", "--max-eng-agents", "1", "--max-ci-agents", "0")
    store.update("sw", state="running")
    monkeypatch.delenv("AGENTIHOOKS_CONTROLLER_TICK_SECONDS", raising=False)
    monkeypatch.setattr(lease, "beat_s", lambda: 0.01)
    spawn, events = rt.spawn, []
    refused = "agentihooks init-agent: the harness did not report its route within 90s"

    def slow(config, lane, name, task):
        if task.get("id") != "t1":
            return spawn(config, lane, name, task)
        for _ in range(5):
            clock[0] += 120000
            events.append(renewed(store, clock))
            if not events[-1]:
                break
        raise SpawnError(refused)

    class Check:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, doc):
            events.append("intent")
            return []

    monkeypatch.setattr(rt, "spawn", slow)
    monkeypatch.setattr(cli.intent, "Check", Check)
    cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    assert events == [True] * 5 + ["intent"]
    assert [(row["state"], row["error"]) for row in store.launches("sw") if row["task"] == "t1"] == [
        ("failed", refused)
    ]
    assert lease.current(store, "sw").epoch == 1
