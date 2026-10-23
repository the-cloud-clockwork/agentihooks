"""State buckets a hive spoke binds; the hub creates them because hive members hold no stream create grant."""

from scripts.events.bus import Bus

NAMES = (
    "spoke-work",
    "spoke-pending",
    "hive-presence",
    "hive-executions",
    "execution-starts",
    "account-slots",
    "hive-slot-accounts",
)


def provision(bus: Bus) -> None:
    for name in NAMES:
        bus.state(name)
