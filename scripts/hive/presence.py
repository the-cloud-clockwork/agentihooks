import json
import time

from scripts.events.bus import Bus

TTL_MS = 45_000


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
