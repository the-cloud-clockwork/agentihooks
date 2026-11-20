import http.client
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
    """Every request across a hangup gets 200 and none waits for the successor to start.

    The successor writes its pid before it serves, so an answer read while the pid file still names the old server
    came from the old server. A sequential client has at most one request in flight when the old server stops
    accepting, so two such answers after the successor appeared show the old server kept accepting while the
    successor started.
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
    events: list[tuple[str, int | str]] = []
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
                    status: int | str = healthy(ledger_port)
                except Exception as exc:
                    status = repr(exc)
                events.append(("old" if (tmp_path / ".server.pid").read_text() == str(first.pid) else "new", status))
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
        worker.join(60)
        assert not worker.is_alive(), (tmp_path / "server.out").read_text()
        assert successor not in (None, first.pid)
        assert healthy(ledger_port) == 200
        answers = [value for kind, value in events if kind != "child"]
        assert answers and set(answers) == {200}, (tmp_path / "server.out").read_text()
        assert ("child", successor) in events, f"the old server {first.pid} never listed {successor} as its child"
        started = [kind for kind, _ in events[events.index(("child", successor)) :]]
        assert started.count("old") >= 2, started
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


@pytest.mark.wall_clock
def test_a_malformed_answer_after_the_successor_is_named_fails_the_reload_test(
    tmp_path: Path, ledger_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """urlopen raises BadStatusLine, which is no OSError, for a status line cut short such as HTTP/1.1 20."""
    real_healthy, test_thread = healthy, threading.current_thread()
    named_pids: list[int | None] = []
    malformed_pids: list[int | None] = []

    def malformed_once_the_successor_is_named(port: int, timeout: float = 30) -> int:
        """Raise once on the hammer thread when the pid file names a server other than the first request's."""
        pid = pid_of(tmp_path)
        named_pids.append(pid)
        successor_named = pid not in (None, named_pids[0])
        if successor_named and not malformed_pids and threading.current_thread() is not test_thread:
            malformed_pids.append(pid)
            raise http.client.BadStatusLine("HTTP/1.1 20")
        return real_healthy(port, timeout)

    monkeypatch.setitem(globals(), "healthy", malformed_once_the_successor_is_named)
    with pytest.raises(AssertionError, match="BadStatusLine"):
        test_a_reload_hands_over_without_a_failed_or_slow_request(tmp_path, ledger_port)
    assert malformed_pids


@pytest.mark.feature("reload-thread-exceptions-fail-tests")
def test_an_unhandled_reload_thread_exception_fails_pytest(tmp_path: Path) -> None:
    probe = tmp_path / "test_reload_thread_plant.py"
    probe.write_text(
        Path(__file__).read_text()
        + """

def test_planted_reload_thread_exception() -> None:
    def read_pid() -> None:
        raise RuntimeError("planted reload pid read failure")

    worker = threading.Thread(target=read_pid)
    worker.start()
    worker.join()
"""
    )
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\nmarkers =\n    wall_clock\n    feature(id)\n")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-c", str(config), f"{probe}::test_planted_reload_thread_exception"],
        cwd=tmp_path,
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": "", "PYTEST_PLUGINS": ""},
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "PytestUnhandledThreadExceptionWarning" in output
    assert "planted reload pid read failure" in output
