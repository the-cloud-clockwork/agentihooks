import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from typing import TextIO
from urllib.parse import urlsplit

from scripts.scheduler.core import Scheduler

ROOT = Path(__file__).resolve().parents[2]
RECORD = "supervisor.json"
DRAIN_S = 30
STARTER = "AGENTIHOOKS_SCHEDULER_STARTER"


class AlreadyRunning(RuntimeError):
    pass


def runtime_dir(environ: Mapping[str, str] | None = None) -> Path:
    from scripts.swarm.keyspace import ROOT
    from scripts.swarm.store import redis_url

    env = os.environ if environ is None else environ
    url = urlsplit(redis_url(env))
    endpoint = (url.scheme, url.hostname, url.port or 6379, url.path or "/0")
    member = env.get("AGENTIHOOKS_HIVE_ID") if env.get("AGENTIHOOKS_DEPLOYMENT", "local") != "local" else ""
    directory = Path.home() / ".cache" / "agentihooks" / "scheduler"
    if endpoint == ("redis", "127.0.0.1", 6379, "/0") and not member:
        return directory
    namespace = f"{ROOT}:hive:{member}" if member else ""
    identity = json.dumps((endpoint, namespace)).encode()
    return directory / hashlib.sha256(identity).hexdigest()


@contextmanager
def machine_lock(name: str, *, environ: Mapping[str, str] | None = None) -> Iterator[TextIO]:
    directory = runtime_dir(environ) if environ is not None else runtime_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{name}.lock").open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadyRunning(f"scheduler {name} is already running") from exc
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()))
            handle.flush()
            yield handle
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


async def run(scheduler: Scheduler, stop: asyncio.Event | None = None) -> None:
    stopped = stop if stop is not None else _on_signals()
    with machine_lock("runner"):
        while not stopped.is_set():
            await asyncio.to_thread(scheduler.run_due)
            try:
                await asyncio.wait_for(stopped.wait(), timeout=1)
            except TimeoutError:
                pass


def _on_signals() -> asyncio.Event:
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    return stopped


def _command(mode: str) -> list[str]:
    return [sys.executable, "-m", "scripts.scheduler.service", mode]


def supervise(stop: Event | None = None) -> None:
    stopped = stop if stop is not None else Event()
    loaded = revision(ROOT)
    starter = int(os.environ.get(STARTER) or 0)
    with machine_lock("supervisor"):
        _identify(loaded)
        while not stopped.is_set():
            child = subprocess.Popen(_command("run"), cwd=Path(__file__).resolve().parents[2])
            while child.poll() is None:
                if stopped.wait(1) or _abandoned(starter):
                    child.terminate()
                    child.wait()
                    return
            if stopped.wait(1) or _abandoned(starter):
                return


def _abandoned(starter: int) -> bool:
    return bool(starter) and os.getppid() != starter


def _identify(loaded: str) -> None:
    record = {"pid": os.getpid(), "root": str(ROOT), "revision": loaded, "started": time.time()}
    (runtime_dir() / RECORD).write_text(json.dumps(record))


def revision(root: Path | str) -> str:
    try:
        shown = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return shown.stdout.strip() if shown.returncode == 0 else ""


def holder(environ: Mapping[str, str]) -> dict | None:
    try:
        with machine_lock("supervisor", environ=environ):
            return None
    except AlreadyRunning:
        pass
    directory = runtime_dir(environ)
    try:
        pid = int((directory / "supervisor.lock").read_text())
    except (OSError, ValueError):
        pid = 0
    try:
        record = json.loads((directory / RECORD).read_text())
    except (OSError, ValueError):
        return {"pid": pid}
    return record if isinstance(record, dict) and record.get("pid") == pid else {"pid": pid}


def drain(pid: int, environ: Mapping[str, str]) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + DRAIN_S
    while holder(environ) is not None:
        if time.monotonic() >= deadline:
            return False
        time.sleep(1)
    return True


def installed() -> bool:
    from scripts.install import install_root

    return install_root().resolve() == ROOT.resolve()


def _spawn_env(active: Mapping[str, str]) -> dict[str, str]:
    spawned = {key: value for key, value in active.items() if key != STARTER}
    if not installed():
        spawned[STARTER] = str(os.getpid())
    return spawned


def ensure_running(environ: Mapping[str, str] | None = None) -> bool:
    active = dict(os.environ if environ is None else environ)
    if not (
        active.get("REDIS_URL") or active.get("AGENTIHOOKS_HIVE_REDIS_URL") or active.get("AGENTIHOOKS_SWARM_REDIS_URL")
    ):
        return False
    try:
        with machine_lock("startup", environ=active):
            with machine_lock("supervisor", environ=active):
                pass
            with (runtime_dir(active) / "scheduler.log").open("ab") as log:
                subprocess.Popen(
                    _command("supervise"),
                    cwd=Path(__file__).resolve().parents[2],
                    env=_spawn_env(active),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
        return True
    except AlreadyRunning:
        return False


def _supervised() -> None:
    stop = Event()
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        for sig in previous:
            signal.signal(sig, lambda *_: stop.set())
        supervise(stop)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentihooks scheduler service")
    parser.add_argument("mode", choices=("run", "supervise"))
    args = parser.parse_args(argv)
    try:
        if args.mode == "run":
            from scripts.swarm.store import redis_client

            asyncio.run(run(Scheduler(redis_client())))
        else:
            _supervised()
    except AlreadyRunning as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
