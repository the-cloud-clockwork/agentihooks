import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "scripts" / "swarm_ledger" / "ledger_server.py"


def child_env():
    return {**os.environ, "PYTHONPATH": str(ROOT)}


def healthy(port, timeout=5):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=timeout) as resp:
        return resp.status


def pid_of(folder):
    try:
        return int((folder / ".server.pid").read_text())
    except (OSError, ValueError):
        return None


def until(check, limit=30):
    deadline = time.monotonic() + limit
    while not check():
        assert time.monotonic() < deadline
        time.sleep(0.02)


@pytest.mark.wall_clock
def test_a_reload_hands_over_without_a_failed_or_slow_request(tmp_path, ledger_port):
    env = {
        **child_env(),
        "LEDGER_DIR": str(tmp_path),
        "LEDGER_PORT": str(ledger_port),
        "SWARM_RELOAD": "0",
    }
    env.pop("LEDGER_RUN_PID", None)
    env.pop("LEDGER_RUN_START", None)
    with (tmp_path / "server.out").open("w") as log:
        first = subprocess.Popen([sys.executable, str(SERVER), "--serve"], env=env, stdout=log, stderr=log)
    successor = None
    results, stop = [], threading.Event()
    try:
        until(lambda: pid_of(tmp_path) == first.pid)

        def hammer():
            while not stop.is_set():
                started = time.monotonic()
                try:
                    results.append((healthy(ledger_port), time.monotonic() - started))
                except OSError as exc:
                    results.append((repr(exc), time.monotonic() - started))
                time.sleep(0.005)

        worker = threading.Thread(target=hammer, daemon=True)
        worker.start()
        time.sleep(0.2)
        os.kill(first.pid, signal.SIGHUP)
        try:
            first.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pytest.fail((tmp_path / "server.out").read_text())
        until(lambda: pid_of(tmp_path) not in (None, first.pid))
        successor = pid_of(tmp_path)
        time.sleep(0.2)
        stop.set()
        worker.join(10)
        assert successor not in (None, first.pid)
        assert healthy(ledger_port) == 200
        assert results and {status for status, _ in results} == {200}, (tmp_path / "server.out").read_text()
        assert max(seconds for _, seconds in results) < 1.0
    finally:
        stop.set()
        for pid in {first.pid, successor} - {None}:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        first.wait(timeout=10)


@pytest.mark.wall_clock
def test_reload_waits_for_pid_publication_after_the_predecessor_exits(tmp_path, ledger_port, monkeypatch):
    processes = []
    spawn, read_pid = subprocess.Popen, pid_of
    stale = []

    def start(*args, **kwargs):
        process = spawn(*args, **kwargs)
        processes.append(process)
        return process

    def delayed_pid(folder):
        if processes[0].returncode is not None and not stale:
            stale.append(processes[0].pid)
            return processes[0].pid
        return read_pid(folder)

    monkeypatch.setattr(subprocess, "Popen", start)
    monkeypatch.setitem(globals(), "pid_of", delayed_pid)
    test_a_reload_hands_over_without_a_failed_or_slow_request(tmp_path, ledger_port)
    assert stale == [processes[0].pid]
