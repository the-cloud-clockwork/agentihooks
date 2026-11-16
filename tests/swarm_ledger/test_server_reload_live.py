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


def healthy(port: int, timeout: float = 30) -> int:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=timeout) as resp:
        return resp.status


def pid_of(folder):
    try:
        return int((folder / ".server.pid").read_text())
    except (OSError, ValueError):
        return None


def children(pid: int) -> set[int]:
    """The processes any thread of pid started, read from each thread's child list; empty once pid has exited."""
    try:
        threads = os.listdir(f"/proc/{pid}/task")
    except OSError:
        return set()
    found: set[int] = set()
    for thread in threads:
        try:
            found.update(map(int, Path(f"/proc/{pid}/task/{thread}/children").read_text().split()))
        except OSError:
            continue
    return found


def until(check, limit=30):
    deadline = time.monotonic() + limit
    while not check():
        assert time.monotonic() < deadline
        time.sleep(0.02)


@pytest.mark.wall_clock
def test_a_reload_hands_over_without_a_failed_or_slow_request(tmp_path: Path, ledger_port: int) -> None:
    """Every request across a hangup gets 200 and the old server keeps accepting while the successor starts.

    A sequential client has at most one request in flight when the old server stops accepting, so two answers
    between the successor appearing and the old server exiting show the old server accepted requests after it
    started the successor.
    """
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
    events: list[tuple[str, object]] = []
    stop = threading.Event()
    try:
        until(lambda: pid_of(tmp_path) == first.pid)
        assert healthy(ledger_port) == 200

        def hammer() -> None:
            seen: set[int] = set()
            while not stop.is_set():
                for child in children(first.pid) - seen:
                    seen.add(child)
                    events.append(("child", child))
                try:
                    events.append(("answer", healthy(ledger_port)))
                except OSError as exc:
                    events.append(("answer", repr(exc)))
                time.sleep(0.005)

        worker = threading.Thread(target=hammer, daemon=True)
        worker.start()
        time.sleep(0.2)
        os.kill(first.pid, signal.SIGHUP)
        try:
            first.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pytest.fail((tmp_path / "server.out").read_text())
        events.append(("exited", first.pid))
        until(lambda: pid_of(tmp_path) not in (None, first.pid))
        successor = pid_of(tmp_path)
        time.sleep(0.2)
        stop.set()
        worker.join(60)
        assert successor not in (None, first.pid)
        assert healthy(ledger_port) == 200
        answers = [value for kind, value in events if kind == "answer"]
        assert answers and set(answers) == {200}, (tmp_path / "server.out").read_text()
        warming = events[events.index(("child", successor)) : events.index(("exited", first.pid))]
        assert [kind for kind, _ in warming].count("answer") >= 2, warming
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
