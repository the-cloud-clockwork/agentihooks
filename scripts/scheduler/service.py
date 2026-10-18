import argparse
import asyncio
import fcntl
import os
import signal
import subprocess
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from threading import Event

from scripts.scheduler.core import Scheduler


class AlreadyRunning(RuntimeError):
    pass


def runtime_dir() -> Path:
    return Path.home() / ".cache" / "agentihooks" / "scheduler"


@contextmanager
def machine_lock(name: str) -> Iterator[None]:
    directory = runtime_dir()
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
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


async def run(scheduler: Scheduler, stop: asyncio.Event | None = None) -> None:
    stopped = stop if stop is not None else asyncio.Event()
    with machine_lock("runner"):
        while not stopped.is_set():
            await asyncio.to_thread(scheduler.run_due)
            try:
                await asyncio.wait_for(stopped.wait(), timeout=1)
            except TimeoutError:
                pass


def _command(mode: str) -> list[str]:
    return [sys.executable, "-m", "scripts.scheduler.service", mode]


def supervise(stop: Event | None = None) -> None:
    stopped = stop if stop is not None else Event()
    with machine_lock("supervisor"):
        while not stopped.is_set():
            child = subprocess.Popen(_command("run"), cwd=Path(__file__).resolve().parents[2])
            while child.poll() is None:
                if stopped.wait(1):
                    child.terminate()
                    child.wait()
                    return
            if stopped.wait(1):
                return


def ensure_running(environ: Mapping[str, str] | None = None) -> bool:
    active = dict(os.environ if environ is None else environ)
    if not (
        active.get("REDIS_URL") or active.get("AGENTIHOOKS_HIVE_REDIS_URL") or active.get("AGENTIHOOKS_SWARM_REDIS_URL")
    ):
        return False
    try:
        with machine_lock("startup"):
            with machine_lock("supervisor"):
                pass
            with (runtime_dir() / "scheduler.log").open("ab") as log:
                subprocess.Popen(
                    _command("supervise"),
                    cwd=Path(__file__).resolve().parents[2],
                    env=active,
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
