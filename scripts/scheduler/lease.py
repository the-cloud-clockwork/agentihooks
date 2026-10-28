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


@dataclass
class Lease:
    name: str
    token: str
    revision: int


def _held(state: StateStore, name: str) -> tuple[dict | None, int | None]:
    entry = state.get(name)
    if entry is None:
        return None, None
    return (json.loads(entry.value) if entry.value else None), entry.revision


def _value(token: str, now: float) -> bytes:
    return json.dumps({"token": token, "expires": now + LEASE_SECONDS}).encode()


def acquire(state: StateStore, name: str, *, now: float | None = None) -> Lease:
    now = time.time() if now is None else now
    holder, revision = _held(state, name)
    if holder is not None and holder["expires"] > now:
        raise RuntimeError("another scheduler is dispatching on this machine")
    token = uuid.uuid4().hex
    try:
        if revision is None:
            written = state.create(name, _value(token, now))
        else:
            written = state.update(name, _value(token, now), revision)
    except StateConflict as exc:
        raise RuntimeError("another scheduler is dispatching on this machine") from exc
    return Lease(name, token, written)


def verify_lease(state: StateStore, lease: Lease, *, now: float | None = None) -> None:
    now = time.time() if now is None else now
    holder, _ = _held(state, lease.name)
    if holder is None or holder["token"] != lease.token or holder["expires"] <= now:
        raise RuntimeError("scheduler lost its dispatch lock")


def renew(state: StateStore, lease: Lease, *, now: float | None = None) -> None:
    now = time.time() if now is None else now
    verify_lease(state, lease, now=now)
    lease.revision = state.update(lease.name, _value(lease.token, now), lease.revision)


def release(state: StateStore, lease: Lease) -> None:
    holder, revision = _held(state, lease.name)
    if holder is None or holder["token"] != lease.token:
        return
    try:
        state.update(lease.name, b"", revision)
    except StateConflict:
        pass


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
