import time
from types import SimpleNamespace

import pytest

from scripts.events.redis_state import RedisStateStore
from scripts.swarm import cli, controller, fencing, lease, timing
from scripts.swarm.store import RedisStore, SwarmConfig, SwarmError
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_delivery import FakeHerdr

pytestmark = pytest.mark.unit


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
    assert fencing.read(store, "sw")["lease"]["expires_at"] - clock[0] > 179000


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
    real, conflicts = fencing.state.for_store(store, fencing.BUCKET), [1]

    class Racing:
        def get(self, key):
            return real.get(key)

        def create(self, key, value):
            return real.create(key, value)

        def update(self, key, value, revision):
            if conflicts:
                conflicts.pop()
                entry = real.get(key)
                doc = fencing._decode(entry.value)
                doc["orphans"]["race"] = doc["orphans"].get("race", 0) + 1
                real.update(key, fencing._encode(doc), entry.revision)
            return real.update(key, value, revision)

    monkeypatch.setattr(fencing.state, "for_store", lambda *args, **kwargs: Racing())
    clock[0] = 70000
    assert lease.renew(store, "sw", held) == lease.Lease("home", 1, 250000)
    assert conflicts == []


def test_a_renewal_within_one_tick_of_the_last_writes_nothing(store, clock):
    held = lease.acquire(store, "sw", "home")
    before = fencing.state.for_store(store, fencing.BUCKET).get("sw").revision
    clock[0] = 61000
    assert lease.renew(store, "sw", held) == held
    assert fencing.state.for_store(store, fencing.BUCKET).get("sw").revision == before
    clock[0] = 61001
    assert lease.renew(store, "sw", held) == lease.Lease("home", 1, 241001)


def test_renew_refuses_a_stolen_lease_without_writing_through_the_cheap_cache(store, clock):
    held = lease.acquire(store, "sw", "home")
    assert held is not None
    before_entry = fencing.state.for_store(store, fencing.BUCKET).get("sw")
    assert before_entry is not None
    before = before_entry.revision
    clock[0] = held.expires_at
    stolen = lease.acquire(store, "sw", "other")
    assert stolen is not None
    clock[0] = stolen.expires_at - 1
    with pytest.raises(SwarmError) as error:
        lease.renew(store, "sw", held)
    assert str(error.value) == "the controller lease is stale"
    after_entry = fencing.state.for_store(store, fencing.BUCKET).get("sw")
    assert after_entry is not None
    assert after_entry.revision == before + 1
    assert lease.current(store, "sw") == stolen


def test_renew_falls_back_to_the_full_document_when_the_cache_is_missing(store, clock, monkeypatch):
    held = lease.acquire(store, "sw", "home")
    assert held is not None
    monkeypatch.setattr(fencing, "read_lease", lambda store, slug: None)
    clock[0] = 61000
    assert lease.renew(store, "sw", held) == held
    clock[0] = 61001
    assert lease.renew(store, "sw", held) == lease.Lease("home", 1, 241001)
    assert lease.current(store, "sw") == lease.Lease("home", 1, 241001)


def test_renew_falls_back_once_another_writer_bumps_the_bucket_revision(store, clock):
    held = lease.acquire(store, "sw", "home")
    assert held is not None
    fencing.write(store, "peer", lambda doc: doc.update(epoch=doc["epoch"] + 1))
    clock[0] = 61000
    assert lease.renew(store, "sw", held) == held
    clock[0] = 61001
    assert lease.renew(store, "sw", held) == lease.Lease("home", 1, 241001)


def test_a_pass_renews_once_and_keeps_one_beat_however_many_runtime_calls_it_makes(store, clock, monkeypatch):
    store.create(SwarmConfig("sw", ".", 1, 0))
    held = lease.acquire(store, "sw", "home")
    renewals, beats = [], []
    renew = lease.renew
    monkeypatch.setattr(lease, "renew", lambda *args: renewals.append(args) or renew(*args))
    monkeypatch.setattr(lease, "beat", lambda store, slug, held, stop: beats.append((slug, held)))
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
    with lease.held_pass(store, "sw", held):
        assert runtime.has_capacity(store.config("sw")) is True
        assert runtime.retire("one", homes=["home"]) == ("one", ["home"])
        assert runtime.spawn(store.config("sw"), "eng", "one", {"id": "t"}) == "placed"
        assert runtime.spawn(store.config("sw"), "eng", "two", {"id": "t"}) == "placed"
    assert renewals == [(store, "sw", held)]
    assert beats == [("sw", held)]


def test_a_pass_whose_lease_was_stolen_refuses_to_start(store, clock):
    store.create(SwarmConfig("sw", ".", 1, 0))
    held = lease.acquire(store, "sw", "home")
    clock[0] += lease.ttl_ms()
    stolen = lease.acquire(store, "sw", "other")
    ran = []
    with pytest.raises(SwarmError) as error, lease.held_pass(store, "sw", held):
        ran.append("pass")
    assert str(error.value) == "the controller lease is stale"
    assert ran == []
    assert lease.PASS.get() is None
    assert lease.current(store, "sw") == stolen


def test_a_pass_fences_each_ledger_write_with_the_lease_of_the_swarm_that_owns_it(store, clock):
    for slug in ("sw", "peer", "idle"):
        store.create(SwarmConfig(slug, ".", 1, 0))
    peer = lease.acquire(store, "peer", "peer-hive")
    store.redis.set(store.key("sw", "control-epoch"), peer.epoch + 4)
    held = lease.acquire(store, "sw", "home")
    assert lease.epoch_for("sw") is None
    with lease.held_pass(store, "sw", held):
        seen = [lease.epoch_for(slug) for slug in ("sw", "peer", "idle")]
        with lease.fencing(77):
            fenced = [lease.epoch_for(slug) for slug in ("sw", "peer")]
    assert seen == [held.epoch, peer.epoch, None]
    assert fenced == [77, 77]
    assert held.epoch != peer.epoch
    assert lease.epoch_for("sw") is None
    assert lease.EPOCH.get() is None


def test_run_tick_slower_than_the_lease_finishes(env, clock, monkeypatch):  # noqa: F811
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    started = clock[0]

    def slow(*args):
        clock[0] += 120000
        return []

    monkeypatch.setattr(cli.phase_planning, "planning_pass", slow)
    monkeypatch.setattr(cli.wake, "maintenance", slow)
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
    assert 179000 < fencing.read(store, "sw")["lease"]["expires_at"] - clock[0] <= 180000


def test_a_shorter_tick_shortens_the_lease_life_to_three_of_its_ticks(store, clock, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_CONTROLLER_TICK_SECONDS", "2.5")
    assert (lease.tick_ms(), lease.ttl_ms()) == (2500, 7500)
    held = lease.acquire(store, "sw", "home")
    assert held == lease.Lease("home", 1, 8500)
    assert 7000 < fencing.read(store, "sw")["lease"]["expires_at"] - clock[0] <= 7500
    clock[0] = 5000
    assert lease.renew(store, "sw", held) == lease.Lease("home", 1, 12500)
    assert 7000 < fencing.read(store, "sw")["lease"]["expires_at"] - clock[0] <= 7500


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
    monkeypatch.setattr(scripts.operator_env, "classifier", lambda environ: [])
    monkeypatch.setattr(controller, "connect", lambda: store)
    monkeypatch.setattr(controller, "run_once", lambda saved: {})
    monkeypatch.setattr(controller, "scheduled_wait", sleep)
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
    with lease.held_pass(store, "sw", held):
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
    assert events[0] == "intent"
    assert events[1:] and all(beat is True for beat in events[1:])
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
    assert events == ["intent"] + [True] * 5
    assert [(row["state"], row["error"]) for row in store.launches("sw") if row["task"] == "t1"] == [
        ("failed", refused)
    ]
    assert lease.current(store, "sw").epoch == 1


def test_a_doctor_timer_slower_than_the_lease_keeps_it_through_the_merge_pass(env, clock, monkeypatch):  # noqa: F811
    from scripts.doctor import cli as doctor
    from scripts.swarm import refresh_job

    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    store.update("sw", template="doctor")
    monkeypatch.delenv("AGENTIHOOKS_CONTROLLER_TICK_SECONDS", raising=False)
    monkeypatch.setattr(lease, "beat_s", lambda: 0.01)
    events = []

    def detectors(*args):
        for _ in range(2):
            clock[0] += 120000
            events.append(renewed(store, clock))
        return []

    monkeypatch.setattr(doctor, "timer", detectors)
    monkeypatch.setattr(cli.ready, "merge_pass", lambda *args: events.append("merge") or [])
    started = clock[0]
    cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    assert events == ["merge"]
    callback = refresh_job.refreshes(store, "sw", ledger, clock[0])[0]
    monkeypatch.setattr(refresh_job, "refreshes", lambda *args: [callback])
    refresh_job.run(store, "sw", ledger, clock[0])
    assert clock[0] - started > lease.ttl_ms()
    assert events == ["merge", True, True]
    assert lease.current(store, "sw").epoch == 1


def test_scheduler_delivery_reaches_the_doctor_timer(env, clock, monkeypatch, tmp_path):  # noqa: F811
    import subprocess
    import sys
    from unittest.mock import Mock

    from scripts.doctor import cli as doctor
    from scripts.scheduler.core import Scheduler
    from scripts.swarm import refresh_job, scheduled_tick, timer

    store, ledger, runtime = env
    monkeypatch.setattr(cli, "now_ms", lambda: clock[0])
    run("sw", "create", "--repo", "/repo")
    store.update("sw", template="doctor")
    monkeypatch.delenv("AGENTIHOOKS_CONTROLLER_TICK_SECONDS", raising=False)
    callback = Mock(return_value=[])
    monkeypatch.setattr(doctor, "timer", lambda *args: callback(*args))
    monkeypatch.setattr(refresh_job, "spawn", lambda slug: refresh_job.refreshes(store, slug, ledger, clock[0])[0]())
    monkeypatch.setattr("scripts.swarm.langfuse.refresh", lambda *args: [])
    monkeypatch.setattr(timer, "entry_point", lambda: "/agentihooks")
    monkeypatch.setattr(scheduled_tick, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(scheduled_tick, "machine_lock", lambda _name: (tmp_path / "swarm-tick.lock").open("a+"))
    monkeypatch.setattr(scheduled_tick, "shutil", SimpleNamespace(which=lambda _name: None))

    def start_tick(command, **_options):
        assert command == ["/agentihooks", "swarm", "tick"]
        cli.run_tick(store, "sw", ledger, runtime, FakeHerdr({}), scheduled=True)
        return SimpleNamespace(wait=lambda _timeout: 0)

    def start_holder(command, **_options):
        assert command == [sys.executable, "-m", "scripts.swarm.scheduled_tick"]
        assert scheduled_tick.main(popen=start_tick) == 0

    monkeypatch.setattr(scheduled_tick, "subprocess", SimpleNamespace(Popen=start_holder, DEVNULL=subprocess.DEVNULL))
    scheduler = Scheduler(RedisStateStore(store.redis, "scheduler"))
    entry = scheduler.get(scheduled_tick.ensure(scheduler))
    due = entry.record["due"]
    assert scheduler.run_due(now=due) == 1
    callback.assert_called_once_with(store, "sw", clock[0], True)
    assert entry.state == "active"
    assert entry.record["due"] > due
    assert scheduler.run_due(now=due) == 0
    callback.assert_called_once()
