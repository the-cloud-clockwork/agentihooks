import os
import signal

import pytest

from hooks.proc import Process
from scripts.swarm_v2 import supervision_processes as trees


def process(pid, parent, start=1, state="S"):
    return Process(pid, parent, pid, pid, start, state, "fixture", ())


def test_descendants_include_nested_children_without_foreign_processes():
    table = {2: process(2, 1), 3: process(3, 2), 4: process(4, 3), 5: process(5, 99)}
    assert trees.descendants(1, table) == {2: table[2], 3: table[3], 4: table[4]}
    assert trees.descendants(99, table) == {5: table[5]}
    assert trees.descendants(100, table) == {}


def test_living_excludes_exporter_tree_and_zombies(monkeypatch):
    table = {2: process(2, 1), 3: process(3, 2), 4: process(4, 1), 5: process(5, 4), 6: process(6, 1, state="Z")}
    monkeypatch.setattr(trees, "processes", lambda: table)
    assert trees.living(1, 4) == {2: table[2], 3: table[3]}
    assert trees.living(1) == {pid: table[pid] for pid in (2, 3, 4, 5)}


def test_send_revalidates_pid_start_and_never_signals_reused_pid(monkeypatch):
    observed = {2: process(2, 1, 12), 3: process(3, 1, 13), 4: process(4, 1, 14)}
    current = {2: process(2, 1, 22), 3: process(3, 1, 13)}
    monkeypatch.setattr(trees, "_process", lambda pid, path: current.get(pid))
    signals = []
    monkeypatch.setattr(os, "kill", lambda pid, signum: signals.append((pid, signum)))
    trees.send(observed, signal.SIGTERM)
    assert signals == [(3, signal.SIGTERM)]


def test_send_accepts_process_disappearance_after_revalidation(monkeypatch):
    child = process(2, 1)
    monkeypatch.setattr(trees, "_process", lambda pid, path: child)

    def disappeared(pid, signum):
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", disappeared)
    trees.send({2: child}, signal.SIGKILL)


def test_reap_is_nonblocking_and_reports_exit_and_signal_status(monkeypatch):
    statuses = iter(((2, 7 << 8), (3, signal.SIGTERM), (0, 0)))

    def wait(pid, flags):
        assert pid == -1 and flags & os.WNOHANG
        return next(statuses)

    monkeypatch.setattr(os, "waitpid", wait)
    assert trees.reap() == [(2, 7), (3, -signal.SIGTERM)]


def test_reap_handles_no_children(monkeypatch):
    def none(pid, flags):
        raise ChildProcessError

    monkeypatch.setattr(os, "waitpid", none)
    assert trees.reap() == []


@pytest.mark.parametrize("remaining,expected", [(True, False), (False, True)])
def test_cleanup_escalates_only_after_deadline(monkeypatch, remaining, expected):
    child = {2: process(2, 1)}
    clock = [0.0]
    monkeypatch.setattr(trees.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(trees.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(trees, "living", lambda root: child if remaining else {})
    signals = []
    monkeypatch.setattr(trees, "send", lambda table, signum: signals.append((dict(table), signum)))
    observations = []
    assert trees.cleanup(1, 0.1, lambda: observations.append(clock[0])) is expected
    assert signals[0] == (child if remaining else {}, signal.SIGTERM)
    assert signals[-1] == (child if remaining else {}, signal.SIGKILL)
    assert observations
    assert clock[0] >= 0.1 if remaining else clock[0] == 0
