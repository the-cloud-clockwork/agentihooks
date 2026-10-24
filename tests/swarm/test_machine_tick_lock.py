import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import herdr_gc, operator_env
from scripts.scheduler.service import AlreadyRunning, machine_lock, runtime_dir
from scripts.swarm import cli, scheduled_tick, tick_reaper, timer

pytestmark = [pytest.mark.unit]

ROOT = Path(__file__).resolve().parents[2]


def test_a_second_tick_start_exits_while_one_holds_the_machine_tick_lock():
    with scheduled_tick.tick_lock({}):
        with pytest.raises(AlreadyRunning), scheduled_tick.tick_lock({}):
            pytest.fail("a second tick ran beside the first")
    with scheduled_tick.tick_lock({}):
        pass


def test_the_tick_the_holder_started_runs_under_the_holders_lock():
    ran = []
    with machine_lock("swarm-tick") as held:
        with scheduled_tick.tick_lock({scheduled_tick.HELD_FD: str(held.fileno())}):
            ran.append(True)
        with pytest.raises(AlreadyRunning), scheduled_tick.tick_lock({}):
            pytest.fail("a tick without the holder's descriptor ran")
    assert ran == [True]


@pytest.mark.parametrize("forged", ["other-file", "closed", "not-a-number"])
def test_a_descriptor_that_is_not_the_tick_lock_is_refused_while_the_holder_runs(tmp_path, forged):
    other = (tmp_path / "other").open("a+")
    value = {"other-file": str(other.fileno()), "closed": "987", "not-a-number": "x"}[forged]
    try:
        with machine_lock("swarm-tick"):
            with pytest.raises(AlreadyRunning), scheduled_tick.tick_lock({scheduled_tick.HELD_FD: value}):
                pytest.fail("a forged descriptor ran a second tick")
    finally:
        other.close()


def test_the_holder_hands_its_locked_descriptor_to_the_tick(monkeypatch):
    launched = []
    monkeypatch.setattr(timer, "entry_point", lambda: "/installed/bin/agentihooks")

    class Tick:
        def wait(self, timeout=None):
            return 0

    def popen(argv, **kwargs):
        fd = int(kwargs["env"][scheduled_tick.HELD_FD])
        launched.append((kwargs["pass_fds"], fd, os.fstat(fd).st_ino))
        return Tick()

    assert scheduled_tick.main(popen) == 0
    ((pass_fds, fd, inode),) = launched
    assert pass_fds == (fd,)
    assert inode == os.stat(runtime_dir() / "swarm-tick.lock").st_ino


def _host_tick(monkeypatch, ticked):
    store = SimpleNamespace(
        slugs=lambda: ["one"],
        config=lambda slug: SimpleNamespace(api_url="", pending_api_url=""),
        redis=SimpleNamespace(hget=lambda *_: None),
        key=lambda slug, tail: f"{slug}:{tail}",
    )
    monkeypatch.setattr(cli, "LedgerClient", lambda: SimpleNamespace(binned=lambda s: False, closed=lambda s: False))
    monkeypatch.setattr(cli.timer, "installed_refusal", lambda: "")
    monkeypatch.setattr(cli, "_reconcile_services", lambda: ticked.append("reconcile"))
    monkeypatch.setattr(operator_env, "fill", lambda environ: None)
    monkeypatch.setattr(operator_env, "classifier", lambda environ: None)
    monkeypatch.setattr(herdr_gc, "run", lambda *args, **kwargs: ticked.append("herdr") or [])
    monkeypatch.setattr(cli, "run_tick", lambda store, slug, scheduled: ticked.append(slug) or [])
    return store


def test_the_host_tick_runs_nothing_while_another_tick_holds_the_machine_lock(monkeypatch, capsys):
    ticked = []
    store = _host_tick(monkeypatch, ticked)
    monkeypatch.setattr(tick_reaper, "reap", lambda: pytest.fail("a tick without the lock reaped"))
    with machine_lock("swarm-tick"):
        cli.cmd_tick(store, None)
    assert ticked == []
    assert capsys.readouterr().err == "another swarm tick is running on this machine\n"


def test_the_host_tick_reaps_and_records_leftovers_before_it_ticks(monkeypatch, capsys):
    ticked = []
    store = _host_tick(monkeypatch, ticked)
    record = tick_reaper.Reaped(pid=4242, age_s=3600, command="python agentihooks swarm tick", signal="SIGTERM")
    monkeypatch.setattr(tick_reaper, "reap", lambda: ticked.append("reap") or [record])
    cli.cmd_tick(store, None)
    assert ticked[:2] == ["reap", "reconcile"]
    assert "one" in ticked
    assert (
        "reaped leftover swarm tick process 4242 after 3600 seconds with SIGTERM: python agentihooks swarm tick"
        in capsys.readouterr().out
    )


def test_two_concurrent_tick_starts_run_one_at_a_time():
    probe = (
        "import sys\n"
        "from scripts.scheduler.service import AlreadyRunning\n"
        "from scripts.swarm import scheduled_tick\n"
        "try:\n"
        "    with scheduled_tick.tick_lock({}):\n"
        "        print('ran', flush=True)\n"
        "        sys.stdin.read()\n"
        "except AlreadyRunning:\n"
        "    print('refused', flush=True)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    starts = [
        subprocess.Popen(
            [sys.executable, "-c", probe], cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
        )
        for _ in range(2)
    ]
    try:
        assert sorted(start.stdout.readline().strip() for start in starts) == ["ran", "refused"]
    finally:
        for start in starts:
            start.stdin.close()
            start.wait(60)
