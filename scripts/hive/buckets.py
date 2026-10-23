"""State buckets a hive spoke binds; the hub creates them because hive members hold no stream create grant."""

from scripts.events.bus import Bus
from scripts.events.config import SCOPED_STATE_BUCKETS as NAMES


def provision(bus: Bus) -> None:
    for name in NAMES:
        bus.state(name)
