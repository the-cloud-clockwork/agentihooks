from collections.abc import Iterator
from contextlib import contextmanager
from threading import Event, Thread
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis import Redis
    from redis.client import Pipeline
    from redis.lock import Lock

LEASE_SECONDS = 30
RENEW_SECONDS = 10


def verify_lease(reader: "Redis | Pipeline", lease: "Lock") -> None:
    token = reader.get(lease.name)
    if isinstance(token, str):
        token = token.encode()
    if lease.local.token is None or token != lease.local.token:
        raise RuntimeError("scheduler lost its dispatch lock")


def _renew(lease: "Lock", stopped: Event) -> None:
    from redis.exceptions import RedisError

    while not stopped.wait(RENEW_SECONDS):
        try:
            lease.reacquire()
        except RedisError:
            return


@contextmanager
def dispatch_lock(redis: "Redis", key: str) -> Iterator["Lock"]:
    from redis.exceptions import LockError

    lease = redis.lock(key, timeout=LEASE_SECONDS, blocking=False, thread_local=False)
    if not lease.acquire():
        raise RuntimeError("another scheduler is dispatching on this machine")
    stopped = Event()
    worker = Thread(target=_renew, args=(lease, stopped), daemon=True)
    worker.start()
    try:
        yield lease
    finally:
        stopped.set()
        worker.join()
        try:
            lease.release()
        except LockError:
            pass
