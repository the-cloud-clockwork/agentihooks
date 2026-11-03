"""Per pass tick executor: tick mode, two lane deadlines, the critical heartbeat, per agent fan out and call caps."""

import fcntl
from collections.abc import Callable, Generator, Iterable
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field
from pathlib import Path
from threading import BoundedSemaphore, Lock, Thread, Timer, local
from typing import IO, TypeVar

from scripts.tick.deadline import Deadline

__all__ = [
    "CRITICAL_S",
    "LIMITS",
    "PARALLEL",
    "SEQUENTIAL",
    "SLOW_LANE",
    "SLOW_S",
    "WORKERS",
    "HostLock",
    "Pass",
    "active",
    "fan_out",
    "limit",
    "running",
]

PARALLEL = "parallel"
SEQUENTIAL = "sequential"
CRITICAL_S = 110.0
SLOW_S = 400.0
WORKERS = 8
SLOW_LANE = "slow"
LATER_GRACE_S = 1.0
LIMITS = {"herdr": 6, "github": 4}

_GATES = {name: BoundedSemaphore(cap) for name, cap in LIMITS.items()}

Item = TypeVar("Item")
Found = TypeVar("Found")


@dataclass(frozen=True)
class Pass:
    """One tick pass: its mode, worker bound and the deadlines of its critical and slow lanes."""

    mode: str
    workers: int
    critical: Deadline
    slow: Deadline
    slots: BoundedSemaphore
    later: list[Thread] = field(default_factory=list[Thread], compare=False)

    def detach(self, work: Callable[[], object]) -> None:
        """Start ``work`` under the caller's copied context on a daemon thread the pass joins when it ends."""
        thread = Thread(target=copy_context().run, args=(work,), name="tick-slow-lane", daemon=True)
        self.later.append(thread)
        thread.start()

    @property
    def parallel(self) -> bool:
        """Return whether steps and per agent work of this pass run concurrently."""
        return self.mode == PARALLEL

    def lane(self, name: str) -> Deadline:
        """Return the deadline of lane ``name``: the slow lane's for slow steps, the critical lane's otherwise."""
        return self.slow if name == SLOW_LANE else self.critical


_ACTIVE: ContextVar[Pass | None] = ContextVar("tick_pass", default=None)


def active() -> Pass | None:
    """Return the pass running in this context, or None outside a pass."""
    return _ACTIVE.get()


class _Beat:
    """Call a heartbeat once, at the critical lane deadline or when the pass ends, whichever comes first."""

    def __init__(self, heartbeat: Callable[[], object], seconds: float) -> None:
        """Start the timer that writes the heartbeat at the critical lane deadline in the caller's context."""
        self._heartbeat, self._lock, self._done = heartbeat, Lock(), False
        self._timer = Timer(seconds, copy_context().run, (self.fire,))
        self._timer.daemon = True
        self._timer.start()

    def fire(self) -> None:
        """Write the heartbeat unless it was already written."""
        with self._lock:
            if self._done:
                return
            self._done = True
        self._heartbeat()

    def close(self) -> None:
        """Stop the timer and write the heartbeat if the pass ended before the critical lane deadline."""
        self._timer.cancel()
        self.fire()


@contextmanager
def running(
    mode: str = PARALLEL,
    workers: int = WORKERS,
    critical_s: float = CRITICAL_S,
    slow_s: float = SLOW_S,
    heartbeat: Callable[[], object] | None = None,
) -> Generator[Pass, None, None]:
    """Run one pass in ``mode``; a parallel pass writes ``heartbeat`` by the critical deadline without slow work."""
    if mode not in (PARALLEL, SEQUENTIAL):
        raise ValueError(f"tick mode must be one of {PARALLEL}, {SEQUENTIAL}")
    if workers < 1:
        raise ValueError("a pass needs at least one worker")
    current = Pass(mode, workers, Deadline.after(critical_s), Deadline.after(slow_s), BoundedSemaphore(workers))
    token = _ACTIVE.set(current)
    beat = _Beat(heartbeat, critical_s) if heartbeat is not None and current.parallel else None
    try:
        yield current
    finally:
        for thread in current.later:
            thread.join(max(0.0, current.slow.remaining) + LATER_GRACE_S)
        _ACTIVE.reset(token)
        if beat is not None:
            beat.close()


def fan_out(action: Callable[[Item], Found], items: Iterable[Item]) -> list[Found]:
    """Return ``action`` of each item in item order, run under copied context on the pass's shared worker slots.

    Every fan out of a pass shares its ``workers`` slots; a fan out started inside a fanned out action runs inline.
    """
    found, values = active(), list(items)
    if found is None or not found.parallel or len(values) < 2 or getattr(_FANNED, "inside", False):
        return [action(value) for value in values]
    executor = ThreadPoolExecutor(max_workers=min(found.workers, len(values)), thread_name_prefix="tick-fan-out")
    try:
        futures = [executor.submit(copy_context().run, _slotted, found.slots, action, value) for value in values]
        wait(futures)
        return [future.result() for future in futures]
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


_FANNED = local()


def _slotted(slots: BoundedSemaphore, action: Callable[[Item], Found], value: Item) -> Found:
    """Return ``action`` of ``value`` while holding one of the pass's worker slots."""
    with slots:
        _FANNED.inside = True
        try:
            return action(value)
        finally:
            del _FANNED.inside


@contextmanager
def limit(resource: str) -> Generator[None, None, None]:
    """Hold one of the host's slots for ``resource`` calls while the block runs."""
    with _GATES[resource]:
        yield


class HostLock:
    """An exclusive lock shared by every swarm on the host through ``flock`` on one file."""

    def __init__(self, path: Path | Callable[[], Path]) -> None:
        """Lock the file at ``path``, or at the path the callable returns when the lock is taken."""
        self._path = path
        self._held = local()
        self._count = 0
        self._counting = Lock()

    def _file(self) -> Path:
        """Return the lock file path, creating its folder."""
        path = self._path() if callable(self._path) else self._path
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def __enter__(self) -> "HostLock":
        """Block until no other holder on the host has the lock, then take it."""
        handle = self._file().open("a")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except BaseException:
            handle.close()
            raise
        stack: list[IO[str]] = getattr(self._held, "stack", [])
        self._held.stack = [*stack, handle]
        with self._counting:
            self._count += 1
        return self

    def __exit__(self, *exc: object) -> None:
        """Release the lock taken last in this thread."""
        handle: IO[str] = self._held.stack.pop()
        with self._counting:
            self._count -= 1
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def locked(self) -> bool:
        """Return whether a thread of this process holds the lock."""
        with self._counting:
            return self._count > 0
