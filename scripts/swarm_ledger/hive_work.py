import threading
from collections.abc import Iterator
from contextlib import contextmanager

# Each hive credential read or token issue decodes a member's whole token map, so this caps the server's memory.
PERMITS = 4
WAIT_S = 5.0
SLOTS = threading.BoundedSemaphore(PERMITS)
BUSY = "The ledger is busy with other hive credential requests"


@contextmanager
def bounded() -> Iterator[None]:
    if not SLOTS.acquire(timeout=WAIT_S):
        from scripts.swarm_ledger.api.errors import APIError

        raise APIError(503, "busy", BUSY)
    try:
        yield
    finally:
        SLOTS.release()
