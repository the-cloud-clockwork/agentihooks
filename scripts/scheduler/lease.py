import json
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Event, Thread

from scripts.events.bus import StateConflict, StateStore

LEASE_SECONDS = 30
RENEW_SECONDS = 10
WAIT_POLL_S = 0.1
WRITE_TRIES = 64


@dataclass
class Lease:
    name: str
    token: str


def _document(state: StateStore, name: str) -> tuple[dict, int | None]:
    entry = state.get(name)
    if entry is None:
        return {}, None
    return (json.loads(entry.value) if entry.value else {}), entry.revision


def _write(state: StateStore, name: str, document: dict, revision: int | None) -> None:
    value = json.dumps(document, sort_keys=True).encode()
    if revision is None:
        state.create(name, value)
    else:
        state.update(name, value, revision)


def holds(document: dict, lease: Lease, now: float) -> bool:
    held = document.get("lease")
    return held is not None and held["token"] == lease.token and held["expires"] > now


def check(document: dict, lease: Lease, now: float | None = None) -> None:
    if not holds(document, lease, time.time() if now is None else now):
        raise RuntimeError("scheduler lost its dispatch lock")


def _change(state: StateStore, name: str, change) -> None:
    for _ in range(WRITE_TRIES):
        document, revision = _document(state, name)
        if change(document) is False:
            return
        try:
            _write(state, name, document, revision)
            return
        except StateConflict:
            continue
    raise StateConflict(f"scheduler lease {name!r} kept changing")


def acquire(state: StateStore, name: str, *, now: float | None = None) -> Lease:
    now = time.time() if now is None else now
    lease = Lease(name, uuid.uuid4().hex)

    def take(document):
        held = document.get("lease")
        if held is not None and held["expires"] > now:
            raise RuntimeError("another scheduler is dispatching on this machine")
        document["lease"] = {"token": lease.token, "expires": now + LEASE_SECONDS}

    _change(state, name, take)
    return lease


def verify_lease(state: StateStore, lease: Lease, *, now: float | None = None) -> None:
    check(_document(state, lease.name)[0], lease, now)


def renew(state: StateStore, lease: Lease, *, now: float | None = None) -> None:
    now = time.time() if now is None else now

    def extend(document):
        check(document, lease, now)
        document["lease"] = {"token": lease.token, "expires": now + LEASE_SECONDS}

    _change(state, lease.name, extend)


def release(state: StateStore, lease: Lease) -> None:
    def drop(document):
        if not holds(document, lease, float("-inf")):
            return False
        document.pop("lease")

    _change(state, lease.name, drop)


def _renew(state: StateStore, lease: Lease, stopped: Event) -> None:
    while not stopped.wait(RENEW_SECONDS):
        try:
            renew(state, lease)
        except (RuntimeError, StateConflict, OSError):
            return


@contextmanager
def dispatch_lock(state: StateStore, name: str) -> Iterator[Lease]:
    lease = acquire(state, name)
    stopped = Event()
    worker = Thread(target=_renew, args=(state, lease, stopped), daemon=True)
    worker.start()
    try:
        yield lease
    finally:
        stopped.set()
        worker.join()
        release(state, lease)


@contextmanager
def held(state: StateStore, name: str, wait_s: float) -> Iterator[Lease]:
    deadline = time.monotonic() + wait_s
    while True:
        try:
            lease = acquire(state, name)
            break
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(WAIT_POLL_S)
    try:
        yield lease
    finally:
        release(state, lease)
