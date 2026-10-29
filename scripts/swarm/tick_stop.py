import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor, wait

DEADLINE_S = 450
GRACE_S = 30
POLL_S = 1.0
SIGTERM_EXIT = 143

STOP = threading.Event()
_HELD: dict[str, Callable[[], None]] = {}
_GUARD = threading.RLock()


class Stopped(BaseException):
    pass


def check() -> None:
    if STOP.is_set():
        raise Stopped


def hold(token: str, release: Callable[[], None]) -> None:
    with _GUARD:
        if not STOP.is_set():
            _HELD[token] = release
            return
    release()
    raise Stopped


def drop(token: str) -> None:
    with _GUARD:
        _HELD.pop(token, None)


def release_all() -> int:
    with _GUARD:
        releases = list(_HELD.values())
        _HELD.clear()
    released = 0
    for release in releases:
        try:
            release()
        except Exception as exc:
            print(f"the swarm tick could not release a tick lock: {type(exc).__name__}: {exc}", file=sys.stderr)
        else:
            released += 1
    return released


def _pending(futures: Iterable[Future], signaled: list[int], until: float) -> set[Future]:
    pending = set(futures)
    while all((pending, not signaled, time.monotonic() < until)):
        pending = wait(pending, timeout=POLL_S).not_done
    return pending


def _report(futures: Iterable[Future]) -> None:
    for future in futures:
        failure = future.exception() if future.done() else None
        if failure is not None and not isinstance(failure, Stopped):
            print(f"a swarm tick failed while the tick stopped: {type(failure).__name__}: {failure}", file=sys.stderr)


def _stop(futures: list[Future], pending: set[Future], why: str) -> set[Future]:
    pending = wait(pending, timeout=GRACE_S).not_done
    released = release_all()
    print(f"the swarm tick stopped {why}; released {released} held tick locks", file=sys.stderr)
    _report(futures)
    return pending


def run(slugs: list[str], work: Callable[[str], object], on_stop: Callable[[], object], spent: float) -> bool:
    STOP.clear()
    signaled: list[int] = []
    previous = signal.signal(signal.SIGTERM, lambda signum, _: signaled.append(signum))
    pool = ThreadPoolExecutor(max_workers=len(slugs))
    futures = [pool.submit(work, slug) for slug in slugs]
    try:
        pending = _pending(futures, signaled, time.monotonic() + DEADLINE_S - spent)
        if not pending and not signaled:
            pool.shutdown()
            for future in futures:
                future.result()
            return False
        STOP.set()
        on_stop()
        by_service = signal.SIGTERM in signaled
        why = "by the service" if by_service else f"at its {DEADLINE_S} second deadline"
        code = SIGTERM_EXIT if by_service else 0
        if _stop(futures, pending, why):
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(code)
        STOP.clear()
        if code:
            raise SystemExit(code)
        return True
    finally:
        signal.signal(signal.SIGTERM, previous)
