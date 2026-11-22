"""The seats of the expert lineages, kept by one pass of the tick.

An expert claims no ledger task, so nothing else places it. For each running lineage this pass launches one expert
on the lineage's seat, follows an accepted handoff with the next expert on the same seat, leaves the next expert a
handoff when the last one was lost without one, and finishes the expert of a lineage that ended.
"""

from dataclasses import replace
from importlib import import_module
from types import SimpleNamespace
from typing import Any

from scripts.handoff import envelope, transfers
from scripts.inbox import exits
from scripts.inbox.store import InboxStore
from scripts.swarm import dispatch_seat, expert_commands, expert_launch, expert_lineage, expert_mission, seat_spawn
from scripts.swarm.store import AgentRecord

LANE = expert_launch.LANE
HELD = ("paused", "stopping", "stopped")
REASON = "exit"
WHAT = "expert of lineage {lineage}"
SPAWNED = "spawned expert {name} on lineage {lineage} as its expert number {number}"
WAITS = "lineage {lineage} waits: {error}"
FAILED = "expert spawn failed on lineage {lineage}: {error}"
ENDED = "ended expert {name}: lineage {lineage} is {state}"
STOPPED = "left when its lineage {lineage} was stopped"
OFFERED = "offered the handoff of lineage {lineage} again: {name} was lost before it confirmed it"
RECOVERED = "wrote the recovery handoff of lineage {lineage}: {name} was lost and left none"
PENDING = "- D{number} unknown: {name} left no account of it; check what is merged and deployed before you rely on it."
MISSION = "- {address} the mission every expert of this lineage works"
RECAP = "- recap:{seat} the last state an expert of this lineage handed over"
RECOVERY = """# Expert handoff
## Mission
{line}
## Done
None
## Pending
{pending}
## Assumptions
None
## Tests
None
## Validation
None
## Stopped at
unknown: {name} held this seat and ended without a handoff, so nothing it did after its launch is recorded here.
## Decisions and promises
unknown: none are recorded outside the seat recap and the open mail of this seat.
## Next
- Read the mission, then establish what each deliverable still needs.
## Read first
{read}
<!-- handoff complete -->
"""
SEAT: Any = SimpleNamespace(
    dispatch_seat=dispatch_seat, envelope=envelope, exits=exits, seat_spawn=seat_spawn, transfers=transfers
)


def run(slug: str, config: Any, store: Any, runtime: Any, now_ms: int) -> list[str]:
    """One pass over the expert seats of a swarm: the lines of what it did. The expert of an ended lineage is
    finished whatever the swarm's state; nothing launches while the swarm is paused, stopping or stopped, and a
    seat that holds a record in any state gets no second expert."""
    experts: list[AgentRecord] = [agent for agent in store.agents(slug) if agent.lane == LANE]
    lineages = expert_lineage.running(store, slug)
    running = {lineage.id for lineage in lineages}
    actions = [_end(slug, store, agent) for agent in experts if agent.state != "finished" and agent.task not in running]
    if config.state in HELD:
        return actions
    seated = {agent.task for agent in experts}
    for lineage in lineages:
        if lineage.id not in seated:
            actions += _launch(slug, config, store, runtime, lineage, now_ms)
    return actions


def _end(slug: str, store: Any, agent: AgentRecord) -> str:
    """Finish the expert of a lineage that ended. A stopped lineage's expert has its open mail withdrawn with the
    reason; a completed lineage's closing steps run again from the kept proof, which finishes what the expert's
    session left undone."""
    lineage = expert_lineage.get(store, slug, agent.task)
    if lineage.state == expert_lineage.COMPLETE:
        expert_commands.close(store, slug, agent, lineage, expert_lineage.proof(store, slug, lineage.id))
    else:
        SEAT.exits.settle(InboxStore(store.redis), agent.name, "", STOPPED.format(lineage=lineage.id))
        expert_commands.returned(store, slug, lineage)
        store.put_agent(slug, replace(agent, state="finished"))
    return ENDED.format(name=agent.name, lineage=lineage.id, state=lineage.state)


def _launch(
    slug: str, config: Any, store: Any, runtime: Any, lineage: expert_lineage.Lineage, now_ms: int
) -> list[str]:
    """Launch the next expert of a running lineage on its empty seat, behind the holds every seat waits on. Its task
    carries the lineage, the mission, its number in the lineage, the stored handoff with the transfer it attaches,
    and the launch an expert is held to."""
    tick: Any = import_module("scripts.swarm.tick")
    what = WHAT.format(lineage=lineage.id)
    held: str = (
        SEAT.seat_spawn.placed_elsewhere(runtime, LANE, {"id": lineage.id})
        or SEAT.seat_spawn.no_slot(config, runtime, what)
        or SEAT.seat_spawn.host_hold(slug, store, now_ms, what)
    )
    if held:
        return [held]
    number = expert_lineage.launches(store, slug, lineage.id) + 1
    left = _left(slug, store, lineage, number, now_ms)

    def prepare(record: AgentRecord) -> dict[str, Any]:
        task: dict[str, Any] = {
            "id": lineage.id,
            "title": lineage.title,
            "mission": lineage.mission,
            "generation": number,
            "handoff": store.handoff(slug, lineage.id),
            "transfer": SEAT.transfers.attach(store, slug, record),
            "requires_harness": expert_launch.HARNESS,
            expert_launch.EFFORT_KEY: lineage.effort,
        }
        primed: dict[str, Any] = tick.primed(store, slug, record.seat, task)
        return primed

    seat = SEAT.seat_spawn.Seat(LANE, lineage.id)
    try:
        record, placed = SEAT.seat_spawn.place_seat(slug, config, store, runtime, seat, now_ms, prepare)
    except SEAT.seat_spawn.SeatFailed as failed:
        store.drop_agent(slug, failed.record.name)
        if SEAT.seat_spawn.room_refused(failed.error):
            SEAT.transfers.detach(store, slug, failed.record)
            return [*left, WAITS.format(lineage=lineage.id, error=failed.error)]
        SEAT.transfers.failed(store, slug, failed.record)
        return [*left, FAILED.format(lineage=lineage.id, error=failed.error)]
    record = tick.placed_record(record, placed)
    store.put_agent(slug, record)
    expert_lineage.launched(store, slug, lineage.id)
    SEAT.dispatch_seat._move_mail_home(store, record)
    return [*left, SPAWNED.format(name=record.name, lineage=lineage.id, number=number)]


def _left(slug: str, store: Any, lineage: expert_lineage.Lineage, number: int, now_ms: int) -> list[str]:
    """Leave the next expert the handoff its seat lacks when the last expert was lost: the stored one it never
    confirmed, offered again, or a recovery handoff that names what is unknown. Nothing is left for the first expert
    of a lineage or while a transfer still waits for its successor."""
    rows: list[dict[str, Any]] = SEAT.transfers.list_transfers(store, slug)
    if number == 1 or any(row["task"] == lineage.id and not row["successor"] for row in rows):
        return []
    name: str = store.seats.occupant(lineage.seat).occupant
    lost = AgentRecord(name, LANE, lineage.id, seat=lineage.seat)
    text: str = store.handoff(slug, lineage.id)
    line = OFFERED if text else RECOVERED
    if not text:
        text = _recovery(slug, store, lineage, name)
        built: dict[str, Any] = SEAT.envelope.build(store, slug, lost, REASON, None, now_ms)
        store.put_handoff(slug, lineage.id, text, seat=lineage.seat, envelope=built)
    SEAT.transfers.record(store, slug, lost, REASON, text, now_ms)
    return [line.format(lineage=lineage.id, name=name)]


def _recovery(slug: str, store: Any, lineage: expert_lineage.Lineage, name: str) -> str:
    """The expert handoff the runtime writes for a seat whose expert was lost: the mission line, every deliverable
    pending and marked unknown, and the mission and the seat recap to read first."""
    mission = expert_lineage.mission(store, slug, lineage)
    count = len(expert_mission.deliverables(mission))
    pending = "\n".join(PENDING.format(number=number, name=name) for number in range(1, count + 1))
    read = [MISSION.format(address=lineage.mission)]
    if store.memory.recaps(lineage.seat):
        read.append(RECAP.format(seat=lineage.seat))
    return RECOVERY.format(line=expert_mission.line(slug, mission), name=name, pending=pending, read="\n".join(read))
