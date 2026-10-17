import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait

DEADLINE_S = 480
GRACE_S = 30
POLL_S = 1.0
SIGTERM_EXIT = 143

STOP = threading.Event()
_HELD: dict[str, Callable[[], None]] = {}
_GUARD = threading.Lock()


class Stopped(BaseException):
    pass


def check() -> None:
    if STOP.is_set():
        raise Stopped


def hold(token: str, release: Callable[[], None]) -> None:
    with _GUARD:
        _HELD[token] = release


def drop(token: str) -> None:
    with _GUARD:
        _HELD.pop(token, None)


def release_all() -> int:
    with _GUARD:
        releases = list(_HELD.values())
        _HELD.clear()
    for release in releases:
        release()
    return len(releases)


def _signaled(stopped: threading.Event) -> Callable:
    def handle(*_):
        stopped.set()
        STOP.set()

    return handle


def run(slugs: list[str], work: Callable[[str], object], on_stop: Callable[[], object] = lambda: None) -> bool:
    STOP.clear()
    signaled = threading.Event()
    previous = signal.signal(signal.SIGTERM, _signaled(signaled))
    pool = ThreadPoolExecutor(max_workers=len(slugs))
    futures = [pool.submit(work, slug) for slug in slugs]
    until = time.monotonic() + DEADLINE_S
    try:
        pending = futures
        while pending and not STOP.is_set() and time.monotonic() < until:
            pending = wait(pending, timeout=min(POLL_S, max(0.0, until - time.monotonic()))).not_done
        if not pending and not signaled.is_set():
            pool.shutdown()
            for future in futures:
                future.result()
            return False
        STOP.set()
        on_stop()
        pending = wait(pending, timeout=GRACE_S).not_done
        released = release_all()
        why = "by the service" if signaled.is_set() else f"at its {DEADLINE_S} second deadline"
        print(f"the swarm tick stopped {why}; released {released} held tick locks", file=sys.stderr, flush=True)
        pool.shutdown(wait=False, cancel_futures=True)
        code = SIGTERM_EXIT if signaled.is_set() else 0
        if pending:
            sys.stdout.flush()
            os._exit(code)
        if code:
            raise SystemExit(code)
        return True
    finally:
        STOP.clear()
        signal.signal(signal.SIGTERM, previous)
