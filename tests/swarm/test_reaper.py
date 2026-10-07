import os
import subprocess
import sys
import time

import pytest

from scripts.swarm import naming, reaper

NAME = "engineer@a1b2c3-0007"
SLEEP = "import time; time.sleep(300)"
CHILD = "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)']); time.sleep(300)"


@pytest.fixture
def plant(tmp_path):
    planted = []

    def start(code=SLEEP, name="", env=None, cwd=None, own_group=True):
        argv = [sys.executable, "-c", code, *(["--name", name] if name else [])]
        environ = {k: v for k, v in os.environ.items() if k != reaper.NAME_KEY}
        proc = subprocess.Popen(argv, env={**environ, **(env or {})}, cwd=cwd, start_new_session=own_group)
        planted.append(proc)
        _until(lambda: os.path.exists(f"/proc/{proc.pid}") and _argv(proc.pid) == argv)
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


def _until(check):
    for _ in range(200):
        if check():
            return
        time.sleep(0.01)
    raise AssertionError("condition never held")


def _gone(proc):
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        return False
    return True


def _group(pgid):
    return [p.pid for p in reaper.processes().values() if p.pgid == pgid and p.state != "Z"]


@pytest.fixture(autouse=True)
def plain_names(monkeypatch):
    monkeypatch.setattr(naming, "resolve_name", lambda name: name)


def test_scratch_homes_are_the_task_folder_in_every_repo(tmp_path):
    for repo in ("agentihooks", "bundle"):
        (tmp_path / repo / "sw-t1").mkdir(parents=True)
    (tmp_path / "agentihooks" / "sw-t10").mkdir()
    (tmp_path / "agentihooks" / "other-t1").mkdir()
    (tmp_path / "loose-file").mkdir()
    assert reaper.scratch_homes("sw", "T1", tmp_path) == [
        (tmp_path / "agentihooks" / "sw-t1").resolve(),
        (tmp_path / "bundle" / "sw-t1").resolve(),
    ]


def test_scratch_homes_ignore_a_file_with_the_folder_name(tmp_path):
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "sw-t1").write_text("")
    assert reaper.scratch_homes("sw", "t1", tmp_path) == []


def test_a_shared_name_retires_only_the_recorded_launch_group(plant):
    launch = plant(CHILD, name=NAME)
    _until(lambda: len(_group(launch.pid)) == 2)
    members = _group(launch.pid)
    other = plant(name=NAME)
    outcome = reaper.retire(NAME, launch.pid, [], wait_s=2)
    assert outcome == reaper.Outcome(tuple(sorted(members)))
    assert _gone(launch)
    assert _group(launch.pid) == []
    assert other.poll() is None


def test_the_launch_process_is_named_by_its_environment(plant):
    launch = plant(env={reaper.NAME_KEY: NAME})
    assert reaper.retire(NAME, launch.pid, [], wait_s=2).ended == (launch.pid,)
    assert _gone(launch)


def test_a_reused_launch_pid_carrying_another_name_is_left_alone(plant):
    stranger = plant(name="engineer@a1b2c3-0008")
    assert reaper.targets(NAME, stranger.pid, []) == reaper.Targets()
    assert reaper.retire(NAME, stranger.pid, [], wait_s=2) == reaper.Outcome()
    assert stranger.poll() is None


def test_a_dead_launch_pid_ends_nothing():
    assert reaper.targets(NAME, 999_999_999, []) == reaper.Targets()
    assert reaper.targets(NAME, 0, []) == reaper.Targets()


def test_a_leftover_proof_daemon_in_the_scratch_home_is_retired(plant, tmp_path):
    home = tmp_path / "scratch" / "repo" / "sw-t1"
    (home / "codex-home").mkdir(parents=True)
    daemon = plant(env={"CODEX_HOME": str(home / "codex-home")})
    by_cwd = plant(cwd=home)
    outside = plant(env={"CODEX_HOME": str(tmp_path / "elsewhere")})
    homes = reaper.scratch_homes("sw", "t1", tmp_path / "scratch")
    outcome = reaper.retire(NAME, 0, homes, wait_s=2)
    assert outcome.ended == tuple(sorted((daemon.pid, by_cwd.pid)))
    assert outcome.refusal == ""
    assert _gone(daemon) and _gone(by_cwd)
    assert outside.poll() is None


@pytest.mark.parametrize("key", reaper.HOME_KEYS)
def test_every_home_variable_marks_a_process_as_running_from_the_home(plant, tmp_path, key):
    home = tmp_path / "sw-t1"
    home.mkdir()
    daemon = plant(env={key: str(home)})
    assert reaper.targets(NAME, 0, [home]).groups == frozenset({daemon.pid})


def test_no_homes_select_no_loose_process(plant, tmp_path):
    plant(cwd=tmp_path)
    assert reaper.targets(NAME, 0, []) == reaper.Targets()


def test_a_scratch_process_inside_a_foreign_group_is_ended_alone(plant, tmp_path):
    home = tmp_path / "sw-t1"
    home.mkdir()
    worker = plant(cwd=home, own_group=False)
    found = reaper.targets(NAME, 0, [home])
    assert found == reaper.Targets(frozenset(), frozenset({worker.pid}))
    assert reaper.end(found, wait_s=2).ended == (worker.pid,)
    assert _gone(worker)


def test_the_caller_group_is_never_signalled_whole(plant):
    launch = plant(name=NAME, own_group=False)
    found = reaper.targets(NAME, launch.pid, [])
    assert found.groups == frozenset()
    assert os.getpid() not in found.singles
    assert launch.pid in found.singles
    assert reaper.end(found, wait_s=2).ended == (launch.pid,)
    assert _gone(launch)


def test_a_refused_signal_is_reported_with_its_process(plant, monkeypatch):
    held = plant(name=NAME, own_group=False)

    def refuse(pid, sig):
        raise PermissionError(1, "Operation not permitted")

    with monkeypatch.context() as patched:
        patched.setattr(reaper.os, "kill", refuse)
        outcome = reaper.end(reaper.Targets(singles=frozenset({held.pid})), wait_s=0.05)
    assert outcome == reaper.Outcome((), held.pid, f"signal to {held.pid} refused: Operation not permitted")
    assert held.poll() is None


def test_a_group_that_vanished_counts_as_ended():
    assert (
        reaper.end(reaper.Targets(frozenset({999_999_999}), frozenset({999_999_998})), wait_s=0.05) == reaper.Outcome()
    )


def test_a_term_resistant_process_is_killed(plant):
    stubborn = plant("import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)", name=NAME)
    outcome = reaper.retire(NAME, stubborn.pid, [], wait_s=0.5)
    assert outcome.ended == (stubborn.pid,)
    assert _gone(stubborn)


def test_reap_ends_a_resumed_duplicate_with_its_group(plant):
    duplicate = plant(CHILD, name=NAME)
    _until(lambda: len(_group(duplicate.pid)) == 2)
    outcome = reaper.reap([duplicate.pid, 999_999_999], wait_s=2)
    assert len(outcome.ended) == 2 and duplicate.pid in outcome.ended
    assert _gone(duplicate)
    assert _group(duplicate.pid) == []
