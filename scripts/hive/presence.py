import json
import time
from collections.abc import Callable

from scripts.events.bus import Bus
from scripts.scheduler import process_interval

BUCKET = "hive-presence"
TTL_MS = 45_000
BEAT_SECONDS = 15


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def beat(bus: Bus, hive: str, at_ms: int) -> int:
    store = bus.state(BUCKET)
    entry = store.get(hive)
    value = json.dumps({"heartbeat_ms": at_ms, "expires_ms": at_ms + TTL_MS}).encode()
    return store.update(hive, value, entry.revision) if entry else store.create(hive, value)


def lost(bus: Bus, hive: str | None, at_ms: int) -> bool:
    if not hive:
        return False
    entry = bus.state(BUCKET).get(hive)
    return entry is not None and json.loads(entry.value)["expires_ms"] <= at_ms


class Heartbeat:
    def __init__(self, bus: Bus, hive: str, clock: Callable[[], int]):
        self.bus, self.hive, self.clock = bus, hive, clock
        self._interval = process_interval.Interval(self._renew, every=BEAT_SECONDS)

    def __enter__(self) -> "Heartbeat":
        self._renew()
        self._interval.start()
        return self

    def _renew(self) -> None:
        beat(self.bus, self.hive, self.clock())

    def check(self) -> None:
        self._interval.check()

    def __exit__(self, *_args: object) -> None:
        self._interval.close()
