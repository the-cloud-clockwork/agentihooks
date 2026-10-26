import json
import time
from collections.abc import Callable
from dataclasses import dataclass

from scripts.events.bus import StateConflict, StateStore

BUCKET = "hub-leases"

Clock = Callable[[], int]


# Expiry is compared across replicas, so the clock is wall time and replicas must keep their clocks in sync.
def wall_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass(frozen=True)
class LeaseState:
    holder: str
    expires_ms: int

    def encode(self) -> bytes:
        return json.dumps({"holder": self.holder, "expires_ms": self.expires_ms}, sort_keys=True).encode()

    @classmethod
    def decode(cls, raw: bytes) -> "LeaseState":
        value = json.loads(raw)
        return cls(holder=value["holder"], expires_ms=value["expires_ms"])


def read(store: StateStore, name: str) -> LeaseState | None:
    entry = store.get(name)
    return None if entry is None else LeaseState.decode(entry.value)


class Lease:
    def __init__(self, store: StateStore, name: str, holder: str, ttl_ms: int, clock: Clock = wall_ms):
        if not isinstance(ttl_ms, int) or isinstance(ttl_ms, bool) or ttl_ms <= 0:
            raise ValueError("lease ttl must be a positive integer")
        self.name = name
        self.holder = holder
        self._store = store
        self._ttl_ms = ttl_ms
        self._clock = clock

    def acquire(self) -> bool:
        entry = self._store.get(self.name)
        now = self._clock()
        claim = LeaseState(self.holder, now + self._ttl_ms).encode()
        try:
            if entry is None:
                self._store.create(self.name, claim)
                return True
            current = LeaseState.decode(entry.value)
            if current.holder != self.holder and current.expires_ms > now:
                return False
            self._store.update(self.name, claim, entry.revision)
            return True
        except StateConflict:
            return False

    def release(self) -> None:
        entry = self._store.get(self.name)
        if entry is None or LeaseState.decode(entry.value).holder != self.holder:
            return
        try:
            self._store.update(self.name, LeaseState(self.holder, 0).encode(), entry.revision)
        except StateConflict:
            return
