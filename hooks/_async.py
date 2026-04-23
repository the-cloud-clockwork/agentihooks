"""Fire-and-forget helper shared across hook handlers.

Rule: every hook that makes a network call, spawns a subprocess, or does
any I/O expected to take >0.5s MUST go through ``fork_and_call``. Hooks
return in <10ms; work proceeds detached in a grandchild that reparents
to init (pid 1 = systemd on laptop, tini on pod) and gets reaped there.

Failures are never propagated back to the hook. They go to:

- Child stdout/stderr → ``~/.agentihooks/logs/async-hooks.log``
- agentihooks log file via ``log()`` where available in the grandchild
- Exit code 124 on timeout (matches ``/usr/bin/timeout`` convention)

This is the canonical non-blocking primitive for the hook layer. Do not
``subprocess.Popen`` from hooks; fork here instead.
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path
from typing import Any, Callable

_LOG_FILE = Path.home() / ".agentihooks" / "logs" / "async-hooks.log"


def fork_and_call(
    func: Callable[..., Any],
    *args: Any,
    timeout_sec: int = 60,
    task_name: str = "hook-task",
    **kwargs: Any,
) -> None:
    """Run ``func(*args, **kwargs)`` in a fully detached grandchild.

    Classic double-fork:
      1. fork → parent waitpids first child (<1ms) → returns to caller
      2. first child ``setsid()`` → new session
      3. first child forks again → exits (grandchild orphan → init)
      4. grandchild: signal.alarm(timeout_sec) + func(...) + os._exit()

    The grandchild redirects stdout + stderr to ``~/.agentihooks/logs/async-hooks.log``
    and closes inherited fds above stderr (Redis / OTel / transcript pipes)
    so parent resources never leak into the detached worker.

    Never raises. Fork failure / import failure / timeout all log + exit.
    """
    _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        pid = os.fork()
    except OSError as e:
        _best_effort_log(f"{task_name}: fork failed: {e}")
        return
    if pid > 0:
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        return

    # First child.
    try:
        os.setsid()
    except OSError:
        pass
    try:
        pid2 = os.fork()
    except OSError:
        os._exit(0)
    if pid2 > 0:
        os._exit(0)

    # Grandchild — detached, runs the actual work.
    try:
        _detach_stdio()
        _close_inherited_fds()
        _install_alarm(timeout_sec, task_name)
        try:
            func(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[async] {task_name}: FAILED: {type(e).__name__}: {e}\n")
            os._exit(1)
        os._exit(0)
    finally:
        os._exit(1)


def _detach_stdio() -> None:
    try:
        log_fd = os.open(str(_LOG_FILE), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
        os.close(log_fd)
        with open(os.devnull, "rb") as devnull:
            os.dup2(devnull.fileno(), 0)
    except OSError:
        pass


def _close_inherited_fds(start: int = 3, end: int = 1024) -> None:
    try:
        os.closerange(start, end)
    except OSError:
        pass


def _install_alarm(timeout_sec: int, task_name: str) -> None:
    def _handler(signum, frame):  # noqa: ARG001
        sys.stderr.write(f"[async] {task_name}: TIMEOUT after {timeout_sec}s\n")
        os._exit(124)

    signal.signal(signal.SIGALRM, _handler)
    signal.alarm(max(1, timeout_sec))


def _best_effort_log(msg: str) -> None:
    """Parent-side logging helper used when we can't even fork."""
    try:
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG_FILE, "a") as f:
            f.write(f"[async] {msg}\n")
    except OSError:
        pass
