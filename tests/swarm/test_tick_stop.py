import json
import os
import signal
import threading
import time
import types

import pytest

from scripts.swarm import cli, controller, lease, tick_stop, timer, timing
from tests.swarm.test_cli import env, run  # noqa: F401

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def cleared():
    yield
    tick_stop.STOP.clear()
    tick_stop._HELD.clear()


def lock_of(store, slug="sw"):
    return store.redis.get(store.key(slug, "tick-lock"))


def blocked_tick(store, monkeypatch, step):
    run("sw", "create", "--repo", ".")
    seen = []

    def providers(*args):
        seen.append(lock_of(store))
        step()

    monkeypatch.setattr(cli, "_tickable", lambda slug: True)
    monkeypatch.setattr(cli.outage, "providers", providers)
    return seen


def tick_each(store, finished=None):
    def work(slug):
        try:
            cli._tick_one(store, slug)
        finally:
            if finished is not None:
                finished.set()

    return work


def no_stop():
    return None


def test_a_service_stop_during_a_tick_releases_its_lock_and_exits_143(env, monkeypatch, capsys):  # noqa: F811
    store, _, _ = env
    woke = []

    def step():
        os.kill(os.getpid(), signal.SIGTERM)
        woke.append(tick_stop.STOP.wait(10))

    seen = blocked_tick(store, monkeypatch, step)
    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(SystemExit) as stopped:
        tick_stop.run(["sw"], tick_each(store), no_stop, 0)
    assert woke == [True]
    assert stopped.value.code == 143
    assert seen[0] is not None
    assert lock_of(store) is None
    assert tick_stop.release_all() == 0
    assert not tick_stop.STOP.is_set()
    assert signal.getsignal(signal.SIGTERM) is before
    assert "the swarm tick stopped by the service; released 0 held tick locks" in capsys.readouterr().err


@pytest.mark.wall_clock
def test_a_stop_releases_a_lock_whose_step_has_not_returned(env, monkeypatch, capsys):  # noqa: F811
    store, _, _ = env
    free, finished, exits = threading.Event(), threading.Event(), []

    def step():
        os.kill(os.getpid(), signal.SIGTERM)
        free.wait(30)

    def hard_exit(code):
        exits.append((code, lock_of(store)))
        raise SystemExit(code)

    seen = blocked_tick(store, monkeypatch, step)
    monkeypatch.setattr(tick_stop, "GRACE_S", 0.2)
    monkeypatch.setattr(tick_stop.os, "_exit", hard_exit)
    began = time.monotonic()
    try:
        with pytest.raises(SystemExit):
            tick_stop.run(["sw"], tick_each(store, finished), no_stop, 0)
        assert time.monotonic() - began < 10
        assert tick_stop.STOP.is_set()
    finally:
        free.set()
        assert finished.wait(10)
    assert seen[0] is not None
    assert exits == [(143, None)]
    assert lock_of(store) is None
    assert "the swarm tick stopped by the service; released 1 held tick locks" in capsys.readouterr().err


def test_the_tick_stops_itself_at_its_deadline_and_releases_its_lock(env, monkeypatch, capsys):  # noqa: F811
    store, _, _ = env
    seen = blocked_tick(store, monkeypatch, lambda: tick_stop.STOP.wait(10))
    settled = threading.Event()
    monkeypatch.setattr(tick_stop, "DEADLINE_S", 0.2)
    assert tick_stop.run(["sw"], tick_each(store), settled.set, 0) is True
    assert seen[0] is not None
    assert lock_of(store) is None
    assert settled.is_set()
    assert not tick_stop.STOP.is_set()
    assert "the swarm tick stopped at its 0.2 second deadline; released 0 held tick locks" in capsys.readouterr().err


@pytest.mark.wall_clock
def test_the_deadline_counts_from_the_tick_start(monkeypatch):
    monkeypatch.setattr(tick_stop, "DEADLINE_S", 30)
    ran = []

    def work(slug):
        ran.append(slug)
        assert tick_stop.STOP.wait(60)

    began = time.monotonic()
    assert tick_stop.run(["a"], work, no_stop, 30) is True
    assert time.monotonic() - began < 10
    assert ran == ["a"]


def test_a_stop_releases_the_locks_of_every_swarm_still_ticking(monkeypatch, capsys):
    free, released, exits = threading.Event(), [], []

    def work(slug):
        tick_stop.hold(slug, lambda: released.append(slug))
        free.wait(30)

    monkeypatch.setattr(tick_stop, "DEADLINE_S", 1)
    monkeypatch.setattr(tick_stop, "GRACE_S", 0.2)

    def hard_exit(code):
        exits.append(code)
        raise SystemExit(code)

    monkeypatch.setattr(tick_stop.os, "_exit", hard_exit)
    try:
        with pytest.raises(SystemExit):
            tick_stop.run(["a", "b"], work, no_stop, 0)
    finally:
        free.set()
    assert sorted(released) == ["a", "b"]
    assert exits == [0]
    assert "released 2 held tick locks" in capsys.readouterr().err


def test_every_swarm_starts_its_tick_before_the_stop(monkeypatch):
    early = []

    def work(slug):
        early.append(not tick_stop.STOP.is_set())
        tick_stop.STOP.wait(10)

    monkeypatch.setattr(tick_stop, "DEADLINE_S", 1)
    assert tick_stop.run([f"s{n}" for n in range(64)], work, no_stop, 0) is True
    assert early == [True] * 64


def test_the_tick_stops_once_its_deadline_is_reached(monkeypatch):
    clock, waits, real_wait = iter([100.0, 100.0]), [], tick_stop.wait

    def timed_wait(futures, timeout):
        waits.append(timeout)
        return real_wait(futures, timeout=timeout)

    monkeypatch.setattr(tick_stop, "time", types.SimpleNamespace(monotonic=lambda: next(clock, 200.0)))
    monkeypatch.setattr(tick_stop, "wait", timed_wait)
    assert tick_stop.run(["a"], lambda slug: tick_stop.STOP.wait(10), no_stop, tick_stop.DEADLINE_S) is True
    assert waits == [tick_stop.GRACE_S]


def test_a_stop_reports_a_swarm_tick_that_failed(monkeypatch, capsys):
    def work(slug):
        if slug == "a":
            raise RuntimeError("broken")
        tick_stop.STOP.wait(10)
        tick_stop.check()

    monkeypatch.setattr(tick_stop, "DEADLINE_S", 0.2)
    assert tick_stop.run(["a", "b"], work, no_stop, 0) is True
    err = capsys.readouterr().err
    assert "a swarm tick failed while the tick stopped: RuntimeError: broken" in err
    assert "Stopped" not in err


def test_a_tick_that_finishes_in_time_returns_without_a_stop():
    ran, before = [], signal.getsignal(signal.SIGTERM)
    assert tick_stop.run(["a", "b"], ran.append, no_stop, 0) is False
    assert sorted(ran) == ["a", "b"]
    assert signal.getsignal(signal.SIGTERM) is before


def test_a_failed_swarm_tick_still_raises():
    def work(slug):
        raise RuntimeError(slug)

    with pytest.raises(RuntimeError, match="^a$"):
        tick_stop.run(["a"], work, no_stop, 0)


def test_release_all_releases_each_held_tick_lock_once():
    import fakeredis

    from scripts.swarm.store import RedisStore

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    held = lease.acquire(store, "sw", "host")
    token = controller.take_tick_lock(store, "sw", held, 600_000)
    tick_stop.hold(token, lambda: controller.release_tick_lock(store, "sw", token))
    tick_stop.hold("gone", lambda: None)
    tick_stop.drop("gone")
    assert tick_stop.release_all() == 1
    assert lock_of(store) is None
    assert tick_stop.release_all() == 0


def test_a_signal_that_lands_while_the_thread_holds_the_guard_still_releases():
    released = []
    tick_stop.hold("token", lambda: released.append("token"))

    def interrupted():
        with tick_stop._GUARD:
            tick_stop.release_all()

    worker = threading.Thread(target=interrupted, daemon=True)
    worker.start()
    worker.join(5)
    assert not worker.is_alive()
    assert released == ["token"]


def test_a_tick_drops_a_lock_the_stop_already_released():
    tick_stop.hold("t", lambda: None)
    assert tick_stop.release_all() == 1
    tick_stop.drop("t")
    assert tick_stop.release_all() == 0


def test_a_failed_release_does_not_stop_the_others(capsys):
    released = []

    def broken():
        raise ConnectionError("redis down")

    tick_stop.hold("a", broken)
    tick_stop.hold("b", lambda: released.append("b"))
    assert tick_stop.release_all() == 1
    assert released == ["b"]
    assert "the swarm tick could not release a tick lock: ConnectionError: redis down" in capsys.readouterr().err


def test_a_lock_taken_after_the_stop_is_released_at_once():
    released = []
    tick_stop.STOP.set()
    with pytest.raises(tick_stop.Stopped):
        tick_stop.hold("late", lambda: released.append("late"))
    assert released == ["late"]
    assert tick_stop.release_all() == 0


def test_a_tick_started_after_the_stop_takes_no_lease(env):  # noqa: F811
    store, ledger, runtime = env
    run("sw", "create", "--repo", ".")
    owner = store.key("sw", "control-owner")
    store.redis.delete(owner)
    tick_stop.STOP.set()
    with pytest.raises(tick_stop.Stopped):
        cli.run_tick(store, "sw", ledger, runtime)
    assert store.redis.get(owner) is None
    assert lock_of(store) is None


def test_a_finished_tick_drops_its_holder_record(env):  # noqa: F811
    store, ledger, runtime = env
    run("sw", "create", "--repo", ".")
    cli.run_tick(store, "sw", ledger, runtime)
    assert lock_of(store) is None
    assert tick_stop.release_all() == 0


def test_the_deadline_and_grace_fit_inside_the_scheduled_tick_timeout():
    from scripts.swarm import scheduled_tick

    assert tick_stop.DEADLINE_S + tick_stop.GRACE_S + tick_stop.POLL_S <= scheduled_tick.TIMEOUT_S - 30


def test_a_stopped_tick_skips_the_herdr_sweep(monkeypatch):
    from scripts import herdr_gc, operator_env

    swept = []
    monkeypatch.setattr(timer, "installed_refusal", lambda: "")
    monkeypatch.setattr(timer, "reconcile", lambda binary: True)
    monkeypatch.setattr(operator_env, "fill", lambda environ: None)
    monkeypatch.setattr(operator_env, "classifier", lambda environ: None)
    monkeypatch.setattr(herdr_gc, "run", lambda *args, **kwargs: swept.append(args) or [])
    monkeypatch.setattr(cli, "run_tick", lambda store, slug, scheduled: tick_stop.STOP.wait(10) and [])
    monkeypatch.setattr(tick_stop, "DEADLINE_S", 0.2)
    monkeypatch.setattr(cli, "_tickable", lambda slug: True)
    store = types.SimpleNamespace(
        slugs=lambda: ["sw"],
        config=lambda slug: types.SimpleNamespace(api_url="", pending_api_url=""),
        redis=types.SimpleNamespace(hget=lambda *_: None),
        key=lambda slug, tail: f"{slug}:{tail}",
    )
    cli.cmd_tick(store, None)
    assert swept == []


def test_a_stopped_step_is_timed_as_stopped_without_a_failure_record(capsys):
    failures = []
    token = timing.ON_FAILURE.set(lambda name, exc: failures.append(name))
    try:
        with timing.tick("sw"), pytest.raises(tick_stop.Stopped), timing.step("x"):
            raise tick_stop.Stopped
    finally:
        timing.ON_FAILURE.reset(token)
    records = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert records[-1]["outcome"] == "stopped"
    assert failures == []
