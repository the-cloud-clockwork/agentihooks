from typing import Any

from scripts.hive import execution_stop, stop_names
from scripts.hive.execution_stop import POLICY, stop_subject
from scripts.hive.stop_names import durable


def hub_stop_on_events(bus: Any, slug: str, hive: str, event: Any) -> bool:
    events = bus.for_swarm(slug)
    # ruleid: stop-on-events-log
    events.events.consumer(durable(slug, hive), stop_subject(slug, hive), POLICY)
    # ruleid: stop-on-events-log
    return events.events.publish(stop_subject(slug, hive), event)


def hive_reads_stops_on_events(bus: Any, slug: str, hive: str) -> Any:
    # ruleid: stop-on-events-log
    return bus.events.consumer(stop_names.durable(slug, hive), execution_stop.stop_subject(slug, hive), POLICY)


def hub_stop_on_stops(bus: Any, slug: str, hive: str, event: Any) -> bool:
    stops = bus.for_swarm(slug).stops
    # ok: stop-on-events-log
    stops.consumer(durable(slug, hive), stop_subject(slug, hive), POLICY)
    # ok: stop-on-events-log
    return stops.publish(stop_subject(slug, hive), event)


def hub_reads_exits_on_events(bus: Any, slug: str) -> Any:
    # ok: stop-on-events-log
    return bus.events.consumer("audit-exits", f"swarm.{slug}.execution.exited")


def hub_stop_through_an_events_variable(bus: Any, slug: str, hive: str, event: Any) -> bool:
    events = bus.for_swarm(slug).events
    # ruleid: stop-on-events-log
    events.consumer(durable(slug, hive), stop_subject(slug, hive), POLICY)
    # ruleid: stop-on-events-log
    return events.publish(stop_subject(slug, hive), event)
