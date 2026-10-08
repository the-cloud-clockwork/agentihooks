import os
import signal
import subprocess
import sys
import time

import pytest

from hooks.proc import Process, processes
from scripts.swarm import naming, reaper

NAME = "engineer@a1b2c3-0007"
SLEEP = "import time; time.sleep(300)"
CHILD = "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)']); time.sleep(300)"
STUBBORN = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)"
GRACEFUL = "import signal, sys, time; signal.signal(signal.SIGTERM, lambda *a: (time.sleep(0.3), sys.exit(0))); time.sleep(300)"


@pytest.fixture
def plant():
    planted = []

    def start(code=SLEEP, name="", env=None, cwd=None, own_group=True):
        argv = [sys.executable, "-c", code, *(["--name", name] if name else [])]
        environ = {k: v for k, v in os.environ.items() if k != reaper.NAME_KEY}
        proc = subprocess.Popen(argv, env={**environ, **(env or {})}, cwd=cwd, start_new_session=own_group)
        planted.append(proc)
        _until(lambda: _argv(proc.pid) == argv)
        return proc

    yield start
    for proc in planted:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def _argv(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            return [item.decode() for item in handle.read().split(b"\0") if item]
    except OSError:
        return []


def _handles_term(pid):
    with open(f"/proc/{pid}/status") as handle:
        masks = [int(line.split()[1], 16) for line in handle if line.startswith(("SigIgn:", "SigCgt:"))]
    return any(mask & 1 << (signal.SIGTERM - 1) for mask in masks)


def _until(check):
    for _ in range(300):
        if check():
            return
        time.sleep(0.01)
    raise AssertionError("condition never held")


def _gone(proc):
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        return False
    return True


def _group(pgid):
    return [p.pid for p in reaper.processes().values() if p.pgid == pgid and p.state != "Z"]


def _fake(pid, pgid, ppid=1):
    return Process(pid=pid, ppid=ppid, pgid=pgid, sid=pgid, start_time=1, state="S", comm="claude", argv=("claude",))


@pytest.fixture(autouse=True)
def plain_names(monkeypatch):
    monkeypatch.setattr(naming, "resolve_name", lambda name: name)


def test_scratch_homes_are_the_task_folder_in_every_repo(tmp_path):
    for repo in ("agentihooks", "bundle"):
        (tmp_path / repo / "sw-t1").mkdir(parents=True)
    (tmp_path / "agentihooks" / "sw-t10").mkdir()
    (tmp_path / "agentihooks" / "other-t1").mkdir()
    assert reaper.scratch_homes("sw", "T1", tmp_path) == [
        (tmp_path / "agentihooks" / "sw-t1").resolve(),
        (tmp_path / "bundle" / "sw-t1").resolve(),
    ]


def test_scratch_homes_default_to_the_scratchpad(tmp_path, monkeypatch):
    (tmp_path / "repo" / "sw-t1").mkdir(parents=True)
    monkeypatch.setattr(reaper, "SCRATCH", tmp_path)
    assert reaper.scratch_homes("sw", "t1") == [(tmp_path / "repo" / "sw-t1").resolve()]


def test_scratch_homes_ignore_a_file_with_the_folder_name(tmp_path):
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "sw-t1").write_text("")
    assert reaper.scratch_homes("sw", "t1", tmp_path) == []


def test_a_shared_name_retires_only_the_recorded_launch_group(plant):
    launch = plant(CHILD, name=NAME)
    _until(lambda: len(_group(launch.pid)) == 2)
    members = _group(launch.pid)
    other = plant(name=NAME)
    assert reaper.retire(NAME, launch.pid, []) == reaper.Outcome(tuple(sorted(members)))
    assert _gone(launch)
    assert _group(launch.pid) == []
    assert other.poll() is None


def test_a_launch_pid_recorded_as_text_is_found(plant):
    launch = plant(name=NAME)
    assert reaper.targets(NAME, str(launch.pid), []) == reaper.Targets(frozenset({launch.pid}))


def test_the_launch_process_is_named_by_its_environment(plant):
    launch = plant(env={reaper.NAME_KEY: NAME})
    assert reaper.retire(NAME, launch.pid, []).ended == (launch.pid,)
    assert _gone(launch)


def test_a_reused_launch_pid_carrying_another_name_is_left_alone(plant):
    stranger = plant(name="engineer@a1b2c3-0008")
    assert reaper.targets(NAME, stranger.pid, []) == reaper.Targets()
    assert reaper.retire(NAME, stranger.pid, []) == reaper.Outcome()
    assert stranger.poll() is None


def test_a_launch_pid_whose_start_time_changed_is_left_alone(plant):
    launch = plant(name=NAME)
    started = processes()[launch.pid].start_time
    assert reaper.targets(NAME, launch.pid, [], started + 1) == reaper.Targets()
    assert reaper.retire(NAME, launch.pid, [], started + 1) == reaper.Outcome()
    assert launch.poll() is None
    assert reaper.targets(NAME, launch.pid, [], started) == reaper.Targets(frozenset({launch.pid}))


@pytest.mark.parametrize("pid", [999_999_999, 0, None])
def test_a_missing_launch_pid_ends_nothing(pid):
    assert reaper.targets(NAME, pid, []) == reaper.Targets()


def test_a_leftover_proof_daemon_in_the_scratch_home_is_retired(plant, tmp_path):
    home = tmp_path / "scratch" / "repo" / "sw-t1"
    (home / "codex-home").mkdir(parents=True)
    daemon = plant(env={"CODEX_HOME": str(home / "codex-home")})
    by_cwd = plant(cwd=home)
    outside = plant(env={"CODEX_HOME": str(tmp_path / "elsewhere")}, cwd=tmp_path)
    homes = reaper.scratch_homes("sw", "t1", tmp_path / "scratch")
    outcome = reaper.retire(NAME, None, homes)
    assert outcome == reaper.Outcome(tuple(sorted((daemon.pid, by_cwd.pid))))
    assert _gone(daemon) and _gone(by_cwd)
    assert outside.poll() is None


@pytest.mark.parametrize("key", reaper.HOME_KEYS)
def test_every_home_variable_marks_a_process_as_running_from_the_home(plant, tmp_path, key):
    home = tmp_path / "sw-t1"
    home.mkdir()
    daemon = plant(env={key: str(home)})
    assert reaper.targets(NAME, None, [home]).groups == frozenset({daemon.pid})


def test_no_homes_select_no_loose_process(plant, tmp_path):
    plant(cwd=tmp_path)
    assert reaper.targets(NAME, None, []) == reaper.Targets()


def test_a_scratch_process_inside_a_foreign_group_is_ended_alone(plant, tmp_path):
    home = tmp_path / "sw-t1"
    home.mkdir()
    worker = plant(cwd=home, own_group=False)
    found = reaper.targets(NAME, None, [home])
    assert found == reaper.Targets(frozenset(), frozenset({worker.pid}))
    assert reaper.end(found).ended == (worker.pid,)
    assert _gone(worker)


def test_the_caller_group_is_never_signalled_whole(plant):
    launch = plant(name=NAME, own_group=False)
    found = reaper.targets(NAME, launch.pid, [])
    assert found == reaper.Targets(frozenset(), frozenset({launch.pid}))
    assert reaper.end(found).ended == (launch.pid,)
    assert _gone(launch)


def test_the_caller_chain_reaches_the_ancestors():
    me = os.getpid()
    table = {me: _fake(me, 7, ppid=70), 70: _fake(70, 7, ppid=71), 71: _fake(71, 71, ppid=1)}
    assert reaper._caller(table) == {me, 70, 71}


def test_a_launch_that_does_not_lead_its_group_ends_with_the_whole_group():
    launch = _fake(10, 9)
    assert reaper._split([], launch, {}) == reaper.Targets(frozenset({9}), frozenset())


def test_a_loose_process_that_does_not_lead_its_group_ends_alone():
    assert reaper._split([_fake(10, 9)], None, {}) == reaper.Targets(frozenset(), frozenset({10}))


@pytest.mark.parametrize("pgid", [0, 1])
def test_group_zero_and_init_are_never_signalled_whole(pgid):
    launch = _fake(10, pgid)
    assert reaper._split([], launch, {}) == reaper.Targets(frozenset(), frozenset({10}))


def test_a_refused_signal_is_reported_with_its_process(plant, monkeypatch):
    held = plant(name=NAME, own_group=False)

    def refuse(pid, sig):
        raise PermissionError(1, "Operation not permitted")

    with monkeypatch.context() as patched:
        patched.setattr(reaper, "TRIES", 1)
        patched.setattr(reaper.os, "kill", refuse)
        outcome = reaper.end(reaper.Targets(singles=frozenset({held.pid})))
    assert outcome == reaper.Outcome((), held.pid, f"signal to {held.pid} refused: Operation not permitted")
    assert held.poll() is None


def test_processes_that_outlive_every_signal_are_named(plant, monkeypatch):
    first, second = plant(name=NAME, own_group=False), plant(name=NAME, own_group=False)
    with monkeypatch.context() as patched:
        patched.setattr(reaper, "TRIES", 1)
        patched.setattr(reaper.os, "kill", lambda pid, sig: None)
        outcome = reaper.end(reaper.Targets(singles=frozenset({first.pid, second.pid})))
    left = sorted((first.pid, second.pid))
    assert outcome == reaper.Outcome((), left[0], f"survived SIGKILL: {left[0]}, {left[1]}")


def test_a_vanished_target_does_not_stop_the_rest(plant, monkeypatch):
    real = plant(name=NAME)
    taken = {p.pgid for p in reaper.processes().values()}
    gone = next(n for n in range(2, real.pid) if n not in taken)
    sent, killpg = [], os.killpg

    def first_gone(pgid, sig):
        sent.append(pgid)
        if pgid == gone:
            raise ProcessLookupError
        killpg(pgid, sig)

    with monkeypatch.context() as patched:
        patched.setattr(reaper.os, "killpg", first_gone)
        outcome = reaper.end(reaper.Targets(frozenset({gone, real.pid})))
    assert sent[:2] == [gone, real.pid]
    assert outcome.ended == (real.pid,)
    assert _gone(real)


def test_a_group_that_vanished_counts_as_ended():
    assert reaper.end(reaper.Targets(frozenset({999_999_999}), frozenset({999_999_998}))) == reaper.Outcome()


def test_a_process_gets_time_to_exit_on_sigterm(plant):
    graceful = plant(GRACEFUL, name=NAME)
    _until(lambda: _handles_term(graceful.pid))
    assert reaper.retire(NAME, graceful.pid, []).ended == (graceful.pid,)
    assert _gone(graceful) and graceful.returncode == 0


def test_a_term_resistant_process_is_killed(plant, monkeypatch):
    monkeypatch.setattr(reaper, "TRIES", 4)
    stubborn = plant(STUBBORN, name=NAME)
    _until(lambda: _handles_term(stubborn.pid))
    assert reaper.retire(NAME, stubborn.pid, []).ended == (stubborn.pid,)
    assert _gone(stubborn) and stubborn.returncode == -9


def test_reap_ends_a_resumed_duplicate_with_its_group(plant):
    duplicate = plant(CHILD, name=NAME)
    _until(lambda: len(_group(duplicate.pid)) == 2)
    members = _group(duplicate.pid)
    assert reaper.reap([duplicate.pid, 999_999_999]) == reaper.Outcome(tuple(sorted(members)))
    assert _gone(duplicate)
    assert _group(duplicate.pid) == []
