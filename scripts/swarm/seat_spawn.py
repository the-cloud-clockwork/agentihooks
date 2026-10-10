"""Spawn one named seat of a swarm, the master or the dispatcher, behind the session slot and host checks."""

from scripts.inbox.seats import seat_address
from scripts.swarm.naming import TYPES
from scripts.swarm.store import AgentRecord


class SeatFailed(Exception):
    def __init__(self, record, error):
        super().__init__(str(error))
        self.record, self.error = record, error


def no_slot(config, runtime, what):
    return "" if runtime.has_capacity(config) else f"no session slot for the {what}, waiting"


def host_hold(slug, store, now_ms, what):
    from scripts.swarm import tick

    host = tick._host_full(slug, store, now_ms)
    return tick._hold(slug, store, f"holding the {what} spawn: {host}") if host else ""


def place(slug, config, store, runtime, lane, now_ms, prepare):
    """Name, record and launch the lane's seat; prepare(record) returns the task its prompt is built from. A failure
    raises SeatFailed with the record still stored, so the caller settles what it began before dropping it."""
    from scripts.swarm import tick

    seat, name = TYPES[lane], store.next_name(slug, lane, now_ms)
    record = AgentRecord(name, lane, seat, started_at=now_ms, state="starting", seat=seat_address(slug, seat))
    store.put_agent(slug, record)
    try:
        store.seats.occupy(record.seat, name, now_ms)
        placed = runtime.spawn(config, lane, name, prepare(record))
    except Exception as exc:
        raise SeatFailed(record, exc) from exc
    tick._spend_host(store, name, now_ms)
    return record, placed
