from typing import Any


def poll(hub: Any, now: int) -> None:
    for swarm, hive in hub.rows():
        # ruleid: hive-loss-without-engine-filter
        if presence.lost(hub.bus.for_swarm(swarm), hive, now):
            hub.exit(swarm, hive)


def poll(hub: Any, now: int) -> None:
    hub.observe(now)
    for swarm, hive in hub.rows():
        # ruleid: hive-loss-without-engine-filter
        lost = now - hub.watching >= presence.TTL_MS and presence.lost(hub.bus.for_swarm(swarm), hive, now)
        hub.settle(swarm, hive, lost)


def poll(hub: Any, now: int) -> None:
    for swarm, hive in hub.rows():
        # ruleid: hive-loss-without-engine-filter
        if hive in hub.hives and presence.lost(hub.bus.for_swarm(swarm), hive, now):
            hub.exit(swarm, hive)


def poll(hub: Any, now: int) -> None:
    for swarm, hive in hub.rows():
        # ok: hive-loss-without-engine-filter
        lost = hub._judged(swarm, now) and presence.lost(hub.bus.for_swarm(swarm), hive, now)
        hub.settle(swarm, hive, lost)


def poll(hub: Any, now: int) -> None:
    events = set(event_slugs(hub.store))
    for swarm, hive in hub.rows():
        # ok: hive-loss-without-engine-filter
        if swarm in events and presence.lost(hub.bus.for_swarm(swarm), hive, now):
            hub.exit(swarm, hive)


def poll(hub: Any, now: int) -> None:
    for swarm, hive in hub.rows():
        if hub.store.config(swarm).engine != EVENTS_ENGINE:
            continue
        # ok: hive-loss-without-engine-filter
        if presence.lost(hub.bus.for_swarm(swarm), hive, now):
            hub.exit(swarm, hive)


def ending(bus: Any, hive: str, now: int) -> bool:
    # ok: hive-loss-without-engine-filter
    return presence.lost(bus, hive, now)
