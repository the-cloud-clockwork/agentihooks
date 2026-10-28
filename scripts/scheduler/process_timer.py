import asyncio
import json
import time
from threading import Event
from typing import TYPE_CHECKING

from scripts.scheduler import service
from scripts.scheduler.core import Scheduler

if TYPE_CHECKING:
    from scripts.events.bus import StateStore

RECHECK_S = 30
POLL_S = 0.2
DONE = ("fired", "cancelled", "stopped")


def _recheck(deadline: float | None) -> float:
    if deadline is None:
        return RECHECK_S
    return min(RECHECK_S, max(0.0, deadline - time.monotonic()))


class Timer:
    def __init__(self, delay: float, *, state: "StateStore | None" = None) -> None:
        self.closed = False
        self.cancelled = Event()
        scheduler = Scheduler(state)
        self.entry = scheduler.timer(owner="process", payload=json.dumps({"delay": delay}), delay=delay)
        self.entry.start()
        try:
            service.ensure_running()
        except BaseException:
            self.close()
            raise

    def _rang(self) -> bool:
        try:
            return self.entry.state in DONE
        except KeyError:
            return True

    def wait(self, timeout: float | None = None) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            recheck = time.monotonic() + _recheck(deadline)
            while time.monotonic() < recheck:
                if self.cancelled.is_set() or self._rang():
                    return
                self.cancelled.wait(min(POLL_S, max(0.0, recheck - time.monotonic())))
            if deadline is not None and time.monotonic() >= deadline:
                return
            service.ensure_running()

    def close(self) -> None:
        if self.closed:
            return
        self.entry.scheduler.retire(self.entry.id)
        self.closed = True

    def __enter__(self) -> "Timer":
        return self

    def __exit__(self, *_args) -> None:
        self.close()


def wait(delay: float, state: "StateStore | None" = None, timeout: float | None = None) -> None:
    with Timer(delay, state=state) as timer:
        timer.wait(timeout)


async def async_wait(delay: float) -> None:
    with Timer(delay) as timer:
        waiting = asyncio.create_task(asyncio.to_thread(timer.wait))
        try:
            await asyncio.shield(waiting)
        finally:
            if not waiting.done():
                timer.cancelled.set()
                await waiting
