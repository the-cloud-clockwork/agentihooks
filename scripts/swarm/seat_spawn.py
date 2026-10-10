"""Spawn one named seat of a swarm, the master or the dispatcher, behind the session slot and host checks."""

from collections.abc import Callable
from dataclasses import dataclass

from scripts.inbox.seats import seat_address
from scripts.swarm.naming import TYPES
from scripts.swarm.store import AgentRecord


class SeatFailed(Exception):
    def __init__(self, record, error):
        super().__init__(str(error))
        self.record, self.error = record, error


def no_slot(config, runtime, what: str) -> str:
    return "" if runtime.has_capacity(config) else f"no session slot for the {what}, waiting"


def host_hold(slug: str, store, now_ms: int, what: str) -> str:
    from scripts.swarm import tick

    host = tick._host_full(slug, store, now_ms)
    return tick._hold(slug, store, f"holding the {what} spawn: {host}") if host else ""


@dataclass(frozen=True)
class Seat:
    lane: str
    name: str


def place(slug: str, config, store, runtime, lane: str, now_ms: int, prepare: Callable[[AgentRecord], dict]):
    """Name, record and launch the lane's seat; prepare(record) returns the task its prompt is built from. A failure
    raises SeatFailed with the record still stored, so the caller settles what it began before dropping it."""
    return place_seat(slug, config, store, runtime, Seat(lane, TYPES[lane]), now_ms, prepare)


def place_seat(slug: str, config, store, runtime, seat: Seat, now_ms: int, prepare: Callable[[AgentRecord], dict]):
    """place for a named seat of a lane, such as a master seat after the lead."""
    from scripts.swarm import tick

    lane, name = seat.lane, store.next_name(slug, seat.lane, now_ms)
    record = AgentRecord(name, lane, seat.name, started_at=now_ms, state="starting", seat=seat_address(slug, seat.name))
    store.put_agent(slug, record)
    try:
        store.seats.occupy(record.seat, name, now_ms)
        placed = runtime.spawn(config, lane, name, prepare(record))
    except Exception as exc:
        raise SeatFailed(record, exc) from exc
    tick._spend_host(store, name, now_ms)
    return record, placed
