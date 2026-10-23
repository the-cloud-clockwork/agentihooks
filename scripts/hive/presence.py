import json
import threading
import time
from collections.abc import Callable

from scripts.events.bus import Bus

TTL_MS = 45_000
BEAT_SECONDS = 15


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def beat(bus: Bus, hive: str, at_ms: int) -> int:
    store = bus.state("hive-presence")
    entry = store.get(hive)
    value = json.dumps({"heartbeat_ms": at_ms, "expires_ms": at_ms + TTL_MS}).encode()
    return store.update(hive, value, entry.revision) if entry else store.create(hive, value)


def lost(bus: Bus, hive: str | None, at_ms: int) -> bool:
    if not hive:
        return False
    entry = bus.state("hive-presence").get(hive)
    return entry is not None and json.loads(entry.value)["expires_ms"] <= at_ms


class Heartbeat:
    def __init__(self, bus: Bus, hive: str, clock: Callable[[], int]):
        self.bus, self.hive, self.clock = bus, hive, clock
        self._stop = threading.Event()
        self._error = None
        self._worker = threading.Thread(target=self._renew, daemon=True)

    def __enter__(self) -> "Heartbeat":
        beat(self.bus, self.hive, self.clock())
        self._worker.start()
        return self

    def _renew(self) -> None:
        try:
            while not self._stop.wait(BEAT_SECONDS):
                beat(self.bus, self.hive, self.clock())
        except Exception as error:
            self._error = error

    def check(self) -> None:
        if self._error is not None:
            raise self._error

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        self._worker.join(timeout=BEAT_SECONDS)
        if self._worker.is_alive():
            raise RuntimeError("hive heartbeat did not stop")
        self.check()
