"""One background exporter per agent session: flushes its trace while the agent works and once more when it exits."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from hooks.observability.agent_trace import TRIGGER_ENV

POLL_SEC = 1.0


@dataclass(frozen=True)
class Budget:
    interval: float = 15.0
    attempt_timeout: float = 5.0
    attempts: int = 3


@dataclass(frozen=True)
class Owner:
    pid: int
    start: int


def budget(environ: Mapping[str, str] | None = None) -> Budget:
    env = os.environ if environ is None else environ

    def number(name: str, default: float) -> float:
        try:
            value = float(env.get(name, ""))
        except ValueError:
            return default
        return value if value > 0 else default

    return Budget(
        interval=number("AGENTIHOOKS_TRACE_FLUSH_INTERVAL_SEC", 15.0),
        attempt_timeout=number("AGENTIHOOKS_TRACE_FLUSH_ATTEMPT_TIMEOUT_SEC", 5.0),
        attempts=int(number("AGENTIHOOKS_TRACE_FLUSH_ATTEMPTS", 3)),
    )


def start_time(pid: int, proc: Path = Path("/proc")) -> int | None:
    try:
        return int((proc / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def alive(owner: Owner, proc: Path = Path("/proc")) -> bool:
    return owner.pid > 1 and start_time(owner.pid, proc) == owner.start


def _base(session_id: str) -> Path:
    from hooks.observability.agent_trace import _cursor_path

    return _cursor_path(session_id).with_suffix("")


def request_path(session_id: str) -> Path:
    return _base(session_id).with_suffix(".request.json")


def owner_path(session_id: str) -> Path:
    return _base(session_id).with_suffix(".owner.json")


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
        json.dump(data, handle)
    Path(handle.name).replace(path)


def _owner(record: dict, prefix: str) -> Owner:
    try:
        return Owner(int(record[f"{prefix}pid"]), int(record[f"{prefix}start"]))
    except (KeyError, TypeError, ValueError):
        return Owner(0, 0)


def request(
    session_id: str,
    transcript_path: str,
    reason: str,
    owner_pid: int | None = None,
    spawn: Callable[[str], None] | None = None,
) -> bool:
    """Record a flush request and start the session's exporter when none is alive; never touches the network."""
    from hooks.observability import otel

    if not session_id or otel.langfuse_exporter_config() is None:
        return False
    if owner_pid is None:
        from hooks.context.account_sessions import agent_pid

        owner_pid = agent_pid()
    previous = _read(request_path(session_id))
    record = {
        "owner_pid": owner_pid,
        "owner_start": start_time(owner_pid),
        "transcript": transcript_path or previous.get("transcript", ""),
        "target": os.environ.get("AGENTIHOOKS_TARGET", "claude"),
        "reason": reason,
        "at": time.time_ns(),
    }
    _write(request_path(session_id), record)
    if alive(_owner(_read(owner_path(session_id)), "supervisor_")):
        return False
    (spawn or _spawn)(session_id)
    return True


def _spawn(session_id: str) -> None:
    from hooks._async import fork_and_call

    fork_and_call(run, session_id, timeout_sec=7 * 24 * 3600, task_name="trace_flush")


def run(session_id: str) -> None:
    signal.alarm(0)
    print(f"trace_flush {session_id}: {supervise(session_id)}", file=sys.stderr)


def _transcript(session_id: str, record: dict) -> str:
    if record.get("transcript"):
        return record["transcript"]
    if record.get("target") == "codex":
        from hooks.targets.normalizer import codex_rollout_path

        return codex_rollout_path(session_id)
    return ""


def attempt(session_id: str, transcript_path: str, timeout: float, trigger: str) -> bool:
    from hooks._async import _worker_env

    env = {**_worker_env(), TRIGGER_ENV: trigger}
    command = [sys.executable, "-m", "hooks.observability.trace_flush", session_id, transcript_path]
    try:
        return subprocess.run(command, env=env, timeout=timeout, stdin=subprocess.DEVNULL).returncode == 0
    except subprocess.TimeoutExpired:
        print(f"trace_flush {session_id}: attempt timed out after {timeout}s", file=sys.stderr)
        return False


def flush_once(session_id: str, transcript_path: str) -> int:
    from hooks.observability.agent_trace import _cursor, export_session

    export_session(session_id, transcript_path)
    return 1 if _cursor(session_id).get("pending") else 0


def _lock(session_id: str, wait: float, clock: Callable[[], float], sleep: Callable[[float], None]):
    path = owner_path(session_id).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a")
    deadline = clock() + wait
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BlockingIOError:
            if clock() >= deadline:
                handle.close()
                return None
            sleep(POLL_SEC)


def supervise(
    session_id: str,
    limits: Budget | None = None,
    send: Callable[[str, str, float, str], bool] = attempt,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    is_alive: Callable[[Owner], bool] = alive,
) -> str:
    limits = limits or budget()
    handle = _lock(session_id, limits.attempts * limits.attempt_timeout + limits.interval, clock, sleep)
    if handle is None:
        return "another exporter owns the session"
    with handle:
        me = {"supervisor_pid": os.getpid(), "supervisor_start": start_time(os.getpid())}
        _write(owner_path(session_id), me)
        flushed_size, seen, due = -1, None, clock()
        while True:
            record = _read(request_path(session_id))
            owner_alive = is_alive(_owner(record, "owner_"))
            fresh = record.get("at") != seen
            if not fresh and owner_alive and clock() < due:
                sleep(POLL_SEC)
                continue
            seen = record.get("at")
            trigger = "final" if not owner_alive else f"request:{record.get('reason', '')}" if fresh else "interval"
            transcript = _transcript(session_id, record)
            size = _size(transcript)
            if transcript and size != flushed_size:
                flushed_size = size if _drain(session_id, transcript, limits, send, trigger) else -1
            due = clock() + limits.interval
            if owner_alive:
                continue
            owner_path(session_id).unlink(missing_ok=True)
            if _read(request_path(session_id)).get("at") == seen:
                return "owner exited"
            _write(owner_path(session_id), me)


def _size(transcript: str) -> int:
    try:
        return Path(transcript).stat().st_size
    except OSError:
        return -1


def _drain(
    session_id: str, transcript: str, limits: Budget, send: Callable[[str, str, float, str], bool], trigger: str
) -> bool:
    return any(send(session_id, transcript, limits.attempt_timeout, trigger) for _ in range(limits.attempts))


if __name__ == "__main__":
    sys.exit(flush_once(sys.argv[1], sys.argv[2]))
