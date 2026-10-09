"""Each swarm timer pass restarts the shared ledger server by its own process id when the code on disk is newer than the
code it loaded, or when its threads or memory pass a limit."""

import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from scripts.inbox.store import InboxStore
from scripts.swarm import ledger_host, ledger_probe, time_left
from scripts.swarm.store import PREFIX
from scripts.swarm_ledger import server_code

PROC = Path("/proc")
THREADS_MAX = 150
RSS_MAX_KB = 1_048_576
WRITES = 3
WRITE_SLOW_S = 2.0
STOP_WAIT_S = 10.0
POLL_S = 0.1
ENSURE_TIMEOUT_S = 30
LOCK_KEY = f"{PREFIX}:ledger-server-restart"
LOCK_MS = 120_000
RUNAWAY_KEY = f"{PREFIX}:ledger-server-runaway"
RUNAWAY_MS = 900_000
STARTED_KEY = f"{PREFIX}:ledger-server-started"
SCRIPT = "ledger_server.py"
ERROR_KEPT = 200
RUNAWAY, STALE = "runaway", "stale"
RUNAWAY_TEXT = "The ledger server was restarted because {why}."
SLOW_TEXT = "The ledger server restarted on new code and its writes are slow: {took}."
DOWN_TEXT = "The ledger server was stopped because {why} and did not start again: {error}."


class Host:
    def __init__(
        self,
        folder: Path,
        proc: Path,
        kill: Callable[[int, int], None],
        run: Callable[..., subprocess.CompletedProcess],
        sleep: Callable[[float], None],
        clock: Callable[[], float],
    ):
        self.folder, self.proc, self.kill, self.run, self.sleep, self.clock = folder, proc, kill, run, sleep, clock


def default_host() -> Host:
    return Host(ledger_host.folder(os.environ), PROC, os.kill, subprocess.run, time.sleep, time.monotonic)


def seen(pid: int, proc: Path) -> dict | None:
    root = proc / str(pid)
    try:
        status = (root / "status").read_text()
        argv = [arg.decode(errors="replace") for arg in (root / "cmdline").read_bytes().split(b"\0") if arg]
    except OSError:
        return None
    fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
    try:
        return {"threads": int(fields["Threads"]), "rss_kb": int(fields.get("VmRSS", "0").split()[0]), "argv": argv}
    except (KeyError, ValueError, IndexError):
        return None


def script(argv: list[str]) -> str | None:
    return next((arg for arg in argv if Path(arg).name == SCRIPT), None)


def launch(pid: int, argv: list[str], proc: Path) -> list[str] | None:
    found = script(argv)
    if found is None:
        return None
    try:
        path = Path(found) if Path(found).is_absolute() else (proc / str(pid) / "cwd").resolve(strict=True) / found
    except OSError:
        return None
    return [argv[0], str(path)] if path.is_file() else None


def alive(pid: int, proc: Path) -> bool:
    try:
        state = (proc / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


def why(folder: Path, pid: int, facts: dict) -> tuple[str, str] | None:
    if facts["threads"] > THREADS_MAX:
        return RUNAWAY, f"it ran {facts['threads']} threads, over the limit of {THREADS_MAX}"
    if facts["rss_kb"] > RSS_MAX_KB:
        return RUNAWAY, f"it held {facts['rss_kb'] // 1024} megabytes, over the limit of {RSS_MAX_KB // 1024}"
    try:
        stale = server_code.stale(folder, pid)
    except OSError:
        return None
    return (STALE, "its code changed on disk") if stale else None


def _signal(host: Host, pid: int, sig: int) -> None:
    try:
        host.kill(pid, sig)
    except OSError:
        pass


def _ours(pid: int, proc: Path) -> bool:
    facts = seen(pid, proc)
    return facts is not None and script(facts["argv"]) is not None


def restart(host: Host, pid: int, command: list[str]) -> str:
    _signal(host, pid, signal.SIGTERM)
    for _ in range(round(STOP_WAIT_S / POLL_S)):
        if not alive(pid, host.proc):
            break
        host.sleep(POLL_S)
    if alive(pid, host.proc) and _ours(pid, host.proc):
        _signal(host, pid, signal.SIGKILL)
    try:
        done = host.run(
            [*command, "--ensure"],
            env={**os.environ, "LEDGER_DIR": str(host.folder)},
            capture_output=True,
            text=True,
            timeout=ENSURE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    if done.returncode == 0:
        return ""
    return ((done.stderr or "").strip() or f"exit code {done.returncode}")[-ERROR_KEPT:]


def writes(ledger, slug: str, inputs: dict, clock: Callable[[], float]) -> list[tuple[float, bool]]:
    took = []
    for _ in range(WRITES):
        started, failed = clock(), False
        try:
            ledger.time_left(slug, inputs.get("slots"), inputs.get("ci_minutes"))
        except ledger_probe.FAILED:
            failed = True
        took.append((clock() - started, failed))
    return took


def described(took: list[tuple[float, bool]]) -> str:
    parts = [f"failed after {seconds:.1f} seconds" if failed else f"{seconds:.1f} seconds" for seconds, failed in took]
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


def slow(took: list[tuple[float, bool]]) -> bool:
    return any(failed or seconds > WRITE_SLOW_S for seconds, failed in took)


def _mail(store, slug: str, text: str) -> None:
    InboxStore(store.redis).send(ledger_probe.SENDER, ledger_probe.master_address(store, slug), text)


def _runaway(store, slug: str, ledger, reason: str) -> list[str]:
    text = RUNAWAY_TEXT.format(why=reason)
    _mail(store, slug, text)
    try:
        ledger.notify(slug, ledger_probe.for_operator(text))
    except ledger_probe.FAILED as exc:
        print(f"ledger restart notice dropped: {exc}", file=sys.stderr)
    return [f"restarted the ledger server because {reason}"]


def _stale(store, slug: str, ledger, runtime, clock: Callable[[], float]) -> list[str]:
    took = writes(ledger, slug, time_left.inputs_of(store, slug, runtime), clock)
    if slow(took):
        _mail(store, slug, SLOW_TEXT.format(took=described(took)))
    return [f"restarted the ledger server on new code; three writes took {described(took)}"]


def _claimed(store, folder: Path, pid: int, kind: str) -> bool:
    if kind == STALE and store.redis.get(STARTED_KEY) == str(pid) and not server_code.recorded(folder, pid):
        return False
    if kind == RUNAWAY and not store.redis.set(RUNAWAY_KEY, pid, nx=True, px=RUNAWAY_MS):
        return False
    return bool(store.redis.set(LOCK_KEY, pid, nx=True, px=LOCK_MS))


def watch(store, slug: str, ledger, runtime, host: Host | None = None) -> list[str]:
    if getattr(ledger, "time_left", None) is None:
        return []
    host = host or default_host()
    pid = ledger_host.server_pid(host.folder)
    facts = seen(pid, host.proc) if pid is not None else None
    command = launch(pid, facts["argv"], host.proc) if facts else None
    found = why(host.folder, pid, facts) if command else None
    if found is None or not _claimed(store, host.folder, pid, found[0]):
        return []
    kind, reason = found
    if error := restart(host, pid, command):
        _mail(store, slug, DOWN_TEXT.format(why=reason, error=error))
        return [f"stopped the ledger server because {reason} and it did not start again: {error}"]
    store.redis.set(STARTED_KEY, ledger_host.server_pid(host.folder) or "")
    return (
        _runaway(store, slug, ledger, reason) if kind == RUNAWAY else _stale(store, slug, ledger, runtime, host.clock)
    )
