"""Master seats two and up: the tick launches a master in each seat of the count that no unfinished or live master
holds, so a raised count fills its seats and a seat whose master ended is filled again."""

from typing import Any

from scripts.inbox.seats import seat_address
from scripts.inbox.store import CLOSED, InboxStore
from scripts.swarm import launch_check, master_start, reaper, seat_spawn
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_v2 import master_seat_alarm, masters
from scripts.swarm_v2.masters_channel import SWARM

ENDED = "stopping"
ASSIGNMENTS = "launch-assignments"
SPAWNED = "spawned master {name} in seat {seat}"
FAILED = "master spawn failed in seat {seat}: {error}"


def run(
    slug: str,
    config: SwarmConfig,
    store: RedisStore,
    runtime: Any,
    doc: dict[str, Any],
    now_ms: int,
    limit: int | None = None,
) -> list[str]:
    """limit caps the seats filled below the count, so a seat the scaling pass is retiring stays empty."""
    if config.state == ENDED:
        return []
    seats = masters.MasterSeats(store.redis)
    count = seats.count(slug) if limit is None else limit
    actions = retire_stuck(slug, store, runtime, now_ms)
    taken = taken_seats(store, slug, runtime.live_names())
    empty = [masters.seat(i) for i in range(2, count + 1) if seat_address(slug, masters.seat(i)) not in taken]
    for seat_name in empty:
        if refused := seat_spawn.placed_elsewhere(runtime, MASTER, {"id": seat_name}):
            actions.append(refused)
            continue
        if refused := seat_spawn.no_slot(config, runtime, MASTER):
            return [*actions, refused]
        actions.append(_place(slug, config, store, runtime, seat_name, now_ms, doc))
    return actions


def stuck(slug: str, agent: AgentRecord, now_ms: int) -> bool:
    """A seat master still starting with no pane past its start deadline will never report."""
    since = agent.launched_at or agent.started_at
    return (
        agent.lane == MASTER
        and not masters.is_lead(slug, agent)
        and agent.state == "starting"
        and not agent.pane_id
        and now_ms - since >= master_start.deadline_ms(agent)
    )


def retire_stuck(slug: str, store: RedisStore, runtime: Any, now_ms: int) -> list[str]:
    """Drop each stuck seat master so its seat is filled again, and move the seat's pending items to the front door."""
    found = [agent for agent in store.agents(slug) if stuck(slug, agent, now_ms)]
    if not found:
        return []
    live: set[str] = runtime.live_names()
    actions: list[str] = []
    inbox = InboxStore(store.redis)
    front = seat_address(slug, MASTER)
    for agent in found:
        if agent.name in live and not runtime.retire(agent, homes=reaper.scratch_homes(slug, agent.task)):
            actions.append(f"could not retire stuck master {agent.name}, retrying next tick")
            continue
        for item in inbox.inbox(agent.seat):
            if item.state not in CLOSED:
                inbox.redirect(item.id, SWARM, front, f"master {agent.name} never started")
        store.drop_agent(slug, agent.name)
        actions.append(f"retired stuck master {agent.name} in seat {agent.seat}")
    return actions


def taken_seats(store, slug: str, live) -> set[str]:
    return {a.seat for a in store.agents(slug) if a.state != "finished" or a.name in live}


def place(slug, config, store, runtime, seat_name, now_ms, prepare):
    """The master launched in seat_name; a failed launch drops its record and raises seat_spawn.SeatFailed."""
    from scripts.swarm import tick

    try:
        record, placed = seat_spawn.place_seat(
            slug, config, store, runtime, seat_spawn.Seat(MASTER, seat_name), now_ms, prepare
        )
    except seat_spawn.SeatFailed as failed:
        store.drop_agent(slug, failed.record.name)
        raise
    record = tick.placed_record(store.execution_occupants(slug).get(record.seat, record), placed)
    store.put_agent(slug, record)
    launch_check.begin(store, slug, record, now_ms)
    store.clear_handoff(slug, seat_name)
    store.redis.hdel(store.key(slug, ASSIGNMENTS), seat_name)
    return record


def _place(slug, config, store, runtime, seat_name, now_ms, doc):
    from scripts.swarm import tick

    seats = masters.MasterSeats(store.redis)
    owners, count = seats.owners(slug, doc), seats.count(slug)

    def prepare(record):
        owns = sorted(item for item, owner in owners.items() if owner == record.seat)
        task = {"id": seat_name, "handoff": store.handoff(slug, seat_name), "owns": owns, "masters": count}
        return tick.primed(store, slug, record.seat, task)

    try:
        record = place(slug, config, store, runtime, seat_name, now_ms, prepare)
    except seat_spawn.SeatFailed as failed:
        master_seat_alarm.failed(store, slug, seat_name, str(failed.error))
        return FAILED.format(seat=failed.record.seat, error=failed.error)
    return SPAWNED.format(name=record.name, seat=record.seat)
