"""Master seats two and up: the tick launches a master in each seat of the count that no unfinished or live master
holds, so a raised count fills its seats and a seat whose master ended is filled again."""

from scripts.inbox.seats import seat_address
from scripts.swarm import launch_check, seat_spawn
from scripts.swarm.store import MASTER
from scripts.swarm_v2 import masters

ENDED = "stopping"
ASSIGNMENTS = "launch-assignments"
SPAWNED = "spawned master {name} in seat {seat}"
FAILED = "master spawn failed in seat {seat}: {error}"


def run(slug: str, config, store, runtime, doc: dict, now_ms: int) -> list[str]:
    if config.state == ENDED:
        return []
    seats = masters.MasterSeats(store.redis)
    count, live = seats.count(slug), runtime.live_names()
    taken = {a.seat for a in store.agents(slug) if a.state != "finished" or a.name in live}
    empty = [masters.seat(i) for i in range(2, count + 1) if seat_address(slug, masters.seat(i)) not in taken]
    actions = []
    for seat_name in empty:
        if refused := (
            seat_spawn.placed_elsewhere(runtime, MASTER, {})
            or seat_spawn.no_slot(config, runtime, MASTER)
            or seat_spawn.host_hold(slug, store, now_ms, MASTER)
        ):
            return [*actions, refused]
        actions.append(_place(slug, config, store, runtime, seat_name, now_ms, doc))
    return actions


def _place(slug, config, store, runtime, seat_name, now_ms, doc):
    from scripts.swarm import tick

    seats = masters.MasterSeats(store.redis)
    owners, count = seats.owners(slug, doc), seats.count(slug)

    def prepare(record):
        owns = sorted(item for item, owner in owners.items() if owner == record.seat)
        task = {"id": seat_name, "handoff": store.handoff(slug, seat_name), "owns": owns, "masters": count}
        return tick.primed(store, slug, record.seat, task)

    try:
        record, placed = seat_spawn.place_seat(
            slug, config, store, runtime, seat_spawn.Seat(MASTER, seat_name), now_ms, prepare
        )
    except seat_spawn.SeatFailed as failed:
        store.drop_agent(slug, failed.record.name)
        return FAILED.format(seat=failed.record.seat, error=failed.error)
    record = tick.placed_record(record, placed)
    store.put_agent(slug, record)
    launch_check.begin(store, slug, record, now_ms)
    store.clear_handoff(slug, seat_name)
    store.redis.hdel(store.key(slug, ASSIGNMENTS), seat_name)
    return SPAWNED.format(name=record.name, seat=record.seat)
