import subprocess
import sys

import pytest

from scripts.scheduler.service import machine_lock, runtime_dir
from scripts.swarm import scheduled_tick

pytestmark = pytest.mark.unit

MINUTE_START = 1_792_000_040.0


def _scheduler(machine="proof"):
    import fakeredis

    from scripts.scheduler.core import Scheduler

    return Scheduler(fakeredis.FakeRedis(decode_responses=True), machine=machine)


def test_ensure_registers_one_minute_cron_with_the_tick_slot():
    scheduler = _scheduler()
    entry_id = scheduled_tick.ensure(scheduler)
    record = scheduler.get(entry_id).record
    assert record["kind"] == "cron"
    assert record["expression"] == "* * * * *"
    assert record["owner"] == "agentihooks swarm tick"
    assert record["payload"] == "swarm tick"
    assert record["state"] == "active"
    assert [(slot["signal"], slot["kind"], slot["reference"]) for slot in record["slots"]] == [
        ("timeout", "callable", "scripts.swarm.scheduled_tick:fire")
    ]
    assert scheduler.redis.get(scheduler.key("swarm-tick")) == entry_id
    assert scheduler.redis.hlen(scheduler.key("entries")) == 1


def test_ensure_reuses_the_live_entry_and_replaces_a_cancelled_or_missing_one():
    scheduler = _scheduler()
    first = scheduled_tick.ensure(scheduler)
    assert scheduled_tick.ensure(scheduler) == first
    assert scheduler.redis.hlen(scheduler.key("entries")) == 1

    scheduler.get(first).cancel()
    second = scheduled_tick.ensure(scheduler)
    assert second != first
    assert scheduler.get(second).state == "active"

    scheduler.redis.hdel(scheduler.key("entries"), second)
    third = scheduled_tick.ensure(scheduler)
    assert third not in (first, second)
    assert scheduler.redis.get(scheduler.key("swarm-tick")) == third


def test_a_pending_entry_counts_as_live():
    scheduler = _scheduler()
    entry_id = scheduled_tick.ensure(scheduler)
    scheduler.update(entry_id, lambda entry: entry.update(state="pending", pending={"slots": []}))
    assert scheduled_tick.ensure(scheduler) == entry_id


def test_ensure_holds_the_registration_lock_and_scopes_keys_per_machine():
    import fakeredis

    from scripts.scheduler.core import Scheduler

    redis = fakeredis.FakeRedis(decode_responses=True)
    locks = []
    real = redis.lock
    redis.lock = lambda name, **kwargs: locks.append((name, kwargs)) or real(name, **kwargs)
    here, there = Scheduler(redis, machine="here"), Scheduler(redis, machine="there")
    assert scheduled_tick.ensure(here) != scheduled_tick.ensure(there)
    assert locks == [
        (here.key("swarm-tick-lock"), {"timeout": 30, "blocking_timeout": 30}),
        (there.key("swarm-tick-lock"), {"timeout": 30, "blocking_timeout": 30}),
    ]
    assert not redis.exists(here.key("swarm-tick-lock"))


def test_the_scheduler_fires_the_tick_holder_every_minute(monkeypatch):
    import time
    from types import SimpleNamespace

    started = []
    popen = lambda argv, **kwargs: started.append((argv, kwargs))  # noqa: E731
    monkeypatch.setattr(scheduled_tick, "subprocess", SimpleNamespace(Popen=popen, DEVNULL=subprocess.DEVNULL))
    scheduler = _scheduler()
    monkeypatch.setattr(time, "time", lambda: MINUTE_START)
    scheduled_tick.ensure(scheduler)
    assert scheduler.run_due(now=MINUTE_START + 30) == 0
    assert scheduler.run_due(now=MINUTE_START + 41) == 1
    assert scheduler.run_due(now=MINUTE_START + 70) == 0
    assert scheduler.run_due(now=MINUTE_START + 101) == 1
    assert scheduler.run_due(now=MINUTE_START + 400) == 1
    assert len(started) == 3
    argv, kwargs = started[0]
    assert argv == [sys.executable, "-m", "scripts.swarm.scheduled_tick"]
    assert kwargs["cwd"] == scheduled_tick.ROOT
    assert (scheduled_tick.ROOT / "scripts" / "swarm" / "scheduled_tick.py").is_file()
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
    assert kwargs["stdout"] is kwargs["stderr"]
    assert kwargs["stdout"].name == str(runtime_dir() / "swarm-tick.log")
    assert kwargs["stdout"].mode == "ab"
    assert kwargs["stdout"].closed


class _Tick:
    def __init__(self, codes):
        self.codes, self.waits, self.signals = list(codes), [], []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        code = self.codes.pop(0)
        if code is None:
            raise subprocess.TimeoutExpired("agentihooks", timeout)
        return code

    def terminate(self):
        self.signals.append("terminate")

    def kill(self):
        self.signals.append("kill")


def test_the_holder_runs_the_installed_tick_in_the_journal_with_a_clean_environment(monkeypatch):
    from scripts.swarm import timer

    launched = []
    monkeypatch.setattr(timer, "entry_point", lambda: "/installed/bin/agentihooks")
    monkeypatch.setattr(scheduled_tick.shutil, "which", {"systemd-cat": "/usr/bin/systemd-cat"}.get)
    monkeypatch.setenv("HOME", "/home/op")
    monkeypatch.setenv("USER", "op")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@abc123-0001")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "t1")
    monkeypatch.setenv("PATH", "/scratch/shim:/usr/bin")
    tick = _Tick([7])
    assert scheduled_tick.main(lambda argv, **kwargs: launched.append((argv, kwargs)) or tick) == 7
    argv, kwargs = launched[0]
    assert argv == ["/usr/bin/systemd-cat", "-t", "agentihooks-swarm", "/installed/bin/agentihooks", "swarm", "tick"]
    assert kwargs["env"]["PATH"] == "/home/op/.local/bin:/home/op/.cargo/bin:/usr/local/bin:/usr/bin:/bin"
    assert kwargs["env"]["HOME"] == "/home/op"
    assert kwargs["env"]["USER"] == "op"
    assert "AGENTIHOOKS_AGENT_NAME" not in kwargs["env"]
    assert "AGENTIHOOKS_SWARM_TASK" not in kwargs["env"]
    assert tick.waits == [540]


def test_without_systemd_cat_the_holder_runs_the_bare_tick(monkeypatch):
    asked = []
    monkeypatch.setattr(scheduled_tick.shutil, "which", lambda name: asked.append(name))
    assert scheduled_tick.command("/bin/agentihooks") == ["/bin/agentihooks", "swarm", "tick"]
    assert asked == ["systemd-cat"]


def test_the_environment_keeps_only_the_user_manager_variables():
    environ = {name: f"value-{name}" for name in scheduled_tick.KEEP}
    environ.update(HOME="/home/op", REDIS_URL="redis://x", AGENTIHOOKS_SWARM="sw")
    kept = scheduled_tick.environment(environ)
    assert set(kept) == {*scheduled_tick.KEEP, "PATH"}
    assert scheduled_tick.environment({"HOME": "/h"}) == {
        "HOME": "/h",
        "PATH": "/h/.local/bin:/h/.cargo/bin:/usr/local/bin:/usr/bin:/bin",
    }


def test_a_fired_minute_starts_no_second_tick_while_one_holds_the_lock(monkeypatch):
    from scripts.swarm import timer

    monkeypatch.setattr(timer, "entry_point", lambda: "/installed/bin/agentihooks")
    launched = []
    popen = lambda argv, **kwargs: launched.append(argv) or _Tick([0])  # noqa: E731
    with machine_lock("swarm-tick"):
        assert scheduled_tick.main(popen) == 0
    assert launched == []
    assert scheduled_tick.main(popen) == 0
    assert scheduled_tick.main(popen) == 0
    assert len(launched) == 2


def test_the_holder_holds_the_tick_lock_while_the_tick_runs(monkeypatch):
    import fcntl

    from scripts.swarm import timer

    monkeypatch.setattr(timer, "entry_point", lambda: "/installed/bin/agentihooks")
    held = []

    def popen(argv, **kwargs):
        with (runtime_dir() / "swarm-tick.lock").open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held.append(True)
        return _Tick([0])

    scheduled_tick.main(popen)
    assert held == [True]


def test_a_tick_past_its_timeout_is_terminated_then_killed():
    tick = _Tick([None, 143])
    assert scheduled_tick._finish(tick) == 143
    assert tick.waits == [540, 90]
    assert tick.signals == ["terminate"]

    tick = _Tick([None, None, -9])
    assert scheduled_tick._finish(tick) == -9
    assert tick.waits == [540, 90, None]
    assert tick.signals == ["terminate", "kill"]
